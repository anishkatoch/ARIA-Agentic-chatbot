import json
import logging

from .config import cfg

logger = logging.getLogger(__name__)

_driver = None          # Neo4j (primary)
_gremlin_client = None  # Azure Cosmos DB Gremlin API (secondary/fallback)


def _get_driver():
    """Primary backend: Neo4j."""
    global _driver
    if _driver is not None:
        return _driver
    if not cfg.neo4j_uri or not cfg.neo4j_password:
        logger.warning("[GRAPH] Neo4j credentials not set - will try Cosmos Gremlin fallback")
        return None
    try:
        from neo4j import GraphDatabase
        _driver = GraphDatabase.driver(cfg.neo4j_uri, auth=(cfg.neo4j_user, cfg.neo4j_password))
        _driver.verify_connectivity()
        logger.info(f"[GRAPH] Connected to Neo4j (primary) - uri={cfg.neo4j_uri}")
        return _driver
    except Exception as e:
        logger.warning(f"[GRAPH] Neo4j connection failed: {e} - will try Cosmos Gremlin fallback")
        return None


def _get_gremlin_client():
    """Secondary/fallback backend: Azure Cosmos DB (Gremlin API). Only
    reached when Neo4j is unset or unreachable."""
    global _gremlin_client
    if _gremlin_client is not None:
        return _gremlin_client
    if not cfg.cosmos_gremlin_endpoint or not cfg.cosmos_gremlin_key:
        logger.warning("[GRAPH] Cosmos Gremlin credentials not set - graph store disabled")
        return None
    try:
        # gremlinpython's transport is asyncio-based and calls
        # run_until_complete() internally; when the host app (e.g. FastAPI)
        # already has an event loop running on this thread, that call raises
        # "Cannot run the event loop while another loop is running".
        # nest_asyncio patches asyncio to allow that nesting - it's already
        # a transitive dependency of gremlinpython for exactly this reason.
        import nest_asyncio
        nest_asyncio.apply()

        from gremlin_python.driver import client, serializer
        _gremlin_client = client.Client(
            cfg.cosmos_gremlin_endpoint,
            "g",
            username=f"/dbs/{cfg.cosmos_gremlin_database}/colls/{cfg.cosmos_gremlin_graph}",
            password=cfg.cosmos_gremlin_key,
            message_serializer=serializer.GraphSONSerializersV2d0(),
        )
        logger.info(f"[GRAPH] Connected to Cosmos DB Gremlin (fallback) - endpoint={cfg.cosmos_gremlin_endpoint}")
        return _gremlin_client
    except Exception as e:
        logger.warning(f"[GRAPH] Cosmos Gremlin connection failed: {e}")
        return None


def _entity_id(name: str, session_id: str) -> str:
    """Deterministic vertex id so Gremlin upserts behave like Neo4j's MERGE."""
    return f"{session_id}::{name}"


def _extract_entities_from_chunk(chunk_text: str, llm) -> list[dict]:
    prompt = (
        "Extract entities and relationships from the text below.\n"
        "Return a JSON array only, no explanation. Each item must have:\n"
        '  {"entity": "name", "type": "Person|Org|Date|Clause|Obligation|Amount|Other", '
        '"relation": "relation-name", "target": "target-entity-name"}\n'
        "If no clear relationship, use relation=mentions and target=document.\n"
        "Extract max 10 items. Text:\n\n"
        f"{chunk_text[:800]}"
    )
    try:
        from langchain_core.messages import HumanMessage
        response = llm.invoke([HumanMessage(content=prompt)])
        raw = response.content.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        logger.warning(f"[GRAPH] Entity extraction failed: {e}")
        return []


# ── Neo4j (primary) implementations ──────────────────────────────────────────

def _build_graph_neo4j(driver, chunks: list[str], session_id: str, llm) -> bool:
    try:
        all_triples = []
        for chunk in chunks:
            triples = _extract_entities_from_chunk(chunk, llm)
            all_triples.extend(triples)

        if not all_triples:
            logger.warning(f"[GRAPH] No entities extracted for session={session_id}")
            return False

        with driver.session() as session:
            for triple in all_triples:
                entity  = str(triple.get("entity", "")).strip()
                etype   = str(triple.get("type", "Other")).strip()
                rel     = str(triple.get("relation", "mentions")).strip().upper().replace(" ", "_")
                target  = str(triple.get("target", "document")).strip()
                if not entity or not target:
                    continue
                session.run(
                    """
                    MERGE (a:Entity {name: $entity, session_id: $sid})
                    SET a.type = $etype
                    MERGE (b:Entity {name: $target, session_id: $sid})
                    MERGE (a)-[r:RELATION {type: $rel, session_id: $sid}]->(b)
                    """,
                    entity=entity, sid=session_id, etype=etype, rel=rel, target=target,
                )

        logger.info(f"[GRAPH] Built graph for session={session_id} - {len(all_triples)} triples")
        return True
    except Exception as e:
        logger.warning(f"[GRAPH] build_graph failed for session={session_id}: {e}")
        return False


def _query_graph_neo4j(driver, question: str, session_id: str) -> str:
    try:
        words = [w.strip(".,;:?!\"'") for w in question.split() if len(w) > 3]
        if not words:
            return ""

        results = []
        with driver.session() as session:
            for word in words[:6]:
                records = session.run(
                    """
                    MATCH (a:Entity {session_id: $sid})-[r:RELATION]->(b:Entity {session_id: $sid})
                    WHERE toLower(a.name) CONTAINS toLower($word)
                       OR toLower(b.name) CONTAINS toLower($word)
                    RETURN a.name AS from, r.type AS rel, b.name AS to
                    LIMIT 10
                    """,
                    sid=session_id, word=word,
                )
                for rec in records:
                    triple = f"{rec['from']} --{rec['rel']}--> {rec['to']}"
                    if triple not in results:
                        results.append(triple)

        if not results:
            logger.info(f"[GRAPH] No graph results for session={session_id}")
            return ""

        graph_ctx = "\n".join(results)
        logger.info(f"[GRAPH] Retrieved {len(results)} triples for session={session_id}")
        return graph_ctx
    except Exception as e:
        logger.warning(f"[GRAPH] query_graph failed for session={session_id}: {e}")
        return ""


def _copy_graph_session_neo4j(driver, old_session_id: str, new_session_id: str) -> bool:
    try:
        with driver.session() as session:
            session.run(
                """
                MATCH (a:Entity {session_id: $old})-[r:RELATION {session_id: $old}]->(b:Entity {session_id: $old})
                MERGE (a2:Entity {name: a.name, session_id: $new})
                SET a2.type = a.type
                MERGE (b2:Entity {name: b.name, session_id: $new})
                MERGE (a2)-[:RELATION {type: r.type, session_id: $new}]->(b2)
                """,
                old=old_session_id, new=new_session_id,
            )
        logger.info(f"[GRAPH] Copied graph session={old_session_id} -> session={new_session_id}")
        return True
    except Exception as e:
        logger.warning(f"[GRAPH] copy_graph_session failed: {e}")
        return False


# ── Azure Cosmos DB Gremlin (secondary/fallback) implementations ────────────

def _upsert_vertex_gremlin(gclient, vid: str, session_id: str, name: str, etype: str) -> None:
    gclient.submit(
        """
        g.V(vid).fold().coalesce(
            unfold(),
            addV('entity').property('id', vid).property('pk', sid)
                .property('name', name).property('type', etype)
        )
        """,
        {"vid": vid, "sid": session_id, "name": name, "etype": etype},
    ).all().result()


def _upsert_edge_gremlin(gclient, a_id: str, b_id: str, edge_id: str, session_id: str, rel: str) -> None:
    gclient.submit(
        """
        g.V(a_id).coalesce(
            outE('relation').hasId(edge_id),
            addE('relation').to(__.V(b_id)).property('id', edge_id).property('pk', sid).property('type', rel)
        )
        """,
        {"a_id": a_id, "b_id": b_id, "edge_id": edge_id, "sid": session_id, "rel": rel},
    ).all().result()


def _build_graph_gremlin(gclient, chunks: list[str], session_id: str, llm) -> bool:
    try:
        all_triples = []
        for chunk in chunks:
            triples = _extract_entities_from_chunk(chunk, llm)
            all_triples.extend(triples)

        if not all_triples:
            logger.warning(f"[GRAPH] No entities extracted for session={session_id}")
            return False

        for triple in all_triples:
            entity  = str(triple.get("entity", "")).strip()
            etype   = str(triple.get("type", "Other")).strip()
            rel     = str(triple.get("relation", "mentions")).strip().upper().replace(" ", "_")
            target  = str(triple.get("target", "document")).strip()
            if not entity or not target:
                continue

            a_id = _entity_id(entity, session_id)
            b_id = _entity_id(target, session_id)
            edge_id = f"{a_id}->{rel}->{b_id}"

            _upsert_vertex_gremlin(gclient, a_id, session_id, entity, etype)
            _upsert_vertex_gremlin(gclient, b_id, session_id, target, "Other")
            _upsert_edge_gremlin(gclient, a_id, b_id, edge_id, session_id, rel)

        logger.info(f"[GRAPH] Built graph (Cosmos fallback) for session={session_id} - {len(all_triples)} triples")
        return True
    except Exception as e:
        logger.warning(f"[GRAPH] Cosmos build_graph failed for session={session_id}: {e}")
        return False


def _query_graph_gremlin(gclient, question: str, session_id: str) -> str:
    words = [w.strip(".,;:?!\"'").lower() for w in question.split() if len(w) > 3]
    if not words:
        return ""

    try:
        edges = gclient.submit(
            """
            g.E().has('pk', sid).project('from', 'rel', 'to')
              .by(outV().values('name'))
              .by('type')
              .by(inV().values('name'))
            """,
            {"sid": session_id},
        ).all().result()
    except Exception as e:
        logger.warning(f"[GRAPH] Cosmos query_graph failed for session={session_id}: {e}")
        return ""

    results = []
    for edge in edges:
        from_name = str(edge.get("from", ""))
        to_name = str(edge.get("to", ""))
        if any(w in from_name.lower() or w in to_name.lower() for w in words[:6]):
            triple = f"{from_name} --{edge.get('rel')}--> {to_name}"
            if triple not in results:
                results.append(triple)
        if len(results) >= 10:
            break

    if not results:
        logger.info(f"[GRAPH] No graph results (Cosmos fallback) for session={session_id}")
        return ""

    graph_ctx = "\n".join(results)
    logger.info(f"[GRAPH] Retrieved {len(results)} triples (Cosmos fallback) for session={session_id}")
    return graph_ctx


def _copy_graph_session_gremlin(gclient, old_session_id: str, new_session_id: str) -> bool:
    try:
        vertices = gclient.submit(
            "g.V().has('pk', old).project('id', 'name', 'type').by(id).by('name').by('type')",
            {"old": old_session_id},
        ).all().result()

        id_map = {}
        for v in vertices:
            new_id = _entity_id(v["name"], new_session_id)
            id_map[v["id"]] = new_id
            _upsert_vertex_gremlin(gclient, new_id, new_session_id, v["name"], v.get("type", "Other"))

        edges = gclient.submit(
            "g.E().has('pk', old).project('outId', 'inId', 'type').by(outV().id()).by(inV().id()).by('type')",
            {"old": old_session_id},
        ).all().result()

        for e in edges:
            a_id = id_map.get(e["outId"])
            b_id = id_map.get(e["inId"])
            if not a_id or not b_id:
                continue
            edge_id = f"{a_id}->{e['type']}->{b_id}"
            _upsert_edge_gremlin(gclient, a_id, b_id, edge_id, new_session_id, e["type"])

        logger.info(f"[GRAPH] Copied graph (Cosmos fallback) session={old_session_id} -> session={new_session_id}")
        return True
    except Exception as e:
        logger.warning(f"[GRAPH] Cosmos copy_graph_session failed: {e}")
        return False


# ── Public API - tries Neo4j (primary) first, falls back to Cosmos Gremlin ──

def build_graph(chunks: list[str], session_id: str, llm) -> bool:
    """Extracts entities/relationships from each chunk (via the given LLM)
    and merges them into the graph store as Entity nodes/RELATION edges
    scoped to session_id. Tries Neo4j first, falls back to Azure Cosmos DB
    (Gremlin API) if Neo4j is unset or unreachable. `llm` is any object
    with a langchain-style .invoke() that returns a .content string - this
    package has no opinion on which LLM provider you use."""
    driver = _get_driver()
    if driver is not None:
        return _build_graph_neo4j(driver, chunks, session_id, llm)

    gclient = _get_gremlin_client()
    if gclient is not None:
        return _build_graph_gremlin(gclient, chunks, session_id, llm)

    return False


def query_graph(question: str, session_id: str) -> str:
    driver = _get_driver()
    if driver is not None:
        return _query_graph_neo4j(driver, question, session_id)

    gclient = _get_gremlin_client()
    if gclient is not None:
        return _query_graph_gremlin(gclient, question, session_id)

    return ""


def copy_graph_session(old_session_id: str, new_session_id: str) -> bool:
    driver = _get_driver()
    if driver is not None:
        return _copy_graph_session_neo4j(driver, old_session_id, new_session_id)

    gclient = _get_gremlin_client()
    if gclient is not None:
        return _copy_graph_session_gremlin(gclient, old_session_id, new_session_id)

    return False

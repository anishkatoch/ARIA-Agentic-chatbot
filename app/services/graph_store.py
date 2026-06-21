import json
import logging
from typing import Optional

from app.config import cfg

logger = logging.getLogger(__name__)

_driver = None


def _get_driver():
    global _driver
    if _driver is not None:
        return _driver
    if not cfg.neo4j_uri or not cfg.neo4j_password:
        logger.warning("[GRAPH] Neo4j credentials not set — graph store disabled")
        return None
    try:
        from neo4j import GraphDatabase
        _driver = GraphDatabase.driver(cfg.neo4j_uri, auth=(cfg.neo4j_user, cfg.neo4j_password))
        _driver.verify_connectivity()
        logger.info(f"[GRAPH] Connected to Neo4j — uri={cfg.neo4j_uri}")
        return _driver
    except Exception as e:
        logger.warning(f"[GRAPH] Neo4j connection failed: {e}")
        return None


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


def build_graph(chunks: list[str], session_id: str, llm) -> bool:
    driver = _get_driver()
    if driver is None:
        return False
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

        logger.info(f"[GRAPH] Built graph for session={session_id} — {len(all_triples)} triples")
        return True
    except Exception as e:
        logger.warning(f"[GRAPH] build_graph failed for session={session_id}: {e}")
        return False


def query_graph(question: str, session_id: str) -> str:
    driver = _get_driver()
    if driver is None:
        return ""
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


def copy_graph_session(old_session_id: str, new_session_id: str) -> bool:
    driver = _get_driver()
    if driver is None:
        return False
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
        logger.info(f"[GRAPH] Copied graph session={old_session_id} → session={new_session_id}")
        return True
    except Exception as e:
        logger.warning(f"[GRAPH] copy_graph_session failed: {e}")
        return False

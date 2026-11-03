import logging

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from langchain_postgres.vectorstores import PGVector
from langchain_chroma import Chroma

from .config import cfg
from .embeddings import get_embeddings

logger = logging.getLogger(__name__)

# One shared connection pool for every session's PGVector store, instead of
# each session opening (and cold-starting) its own pool against Postgres.
_db_engine: Engine | None = None


def get_db_engine() -> Engine:
    global _db_engine
    if _db_engine is None:
        _db_engine = create_engine(cfg.db_url, pool_pre_ping=True)
    return _db_engine


# Once a session has data it only ever gains more (nothing deletes documents
# from a live session), so "has data" never flips back to False - caching
# only the True result sidesteps needing to invalidate this from every
# upload/ingest call site.
_has_data_cache: dict[str, bool] = {}


def session_has_data(session_id: str) -> bool:
    """Return True if the session's vector store has at least one document."""
    if _has_data_cache.get(session_id):
        return True
    try:
        store = get_vector_store(session_id)
        if hasattr(store, "_collection"):
            # Chroma - local count, no embedding call needed.
            result = store._collection.count() > 0
        else:
            result = len(store.similarity_search("a", k=1)) > 0
    except Exception:
        result = False
    if result:
        _has_data_cache[session_id] = True
    return result


# Connecting PGVector/Chroma re-runs schema setup (CREATE EXTENSION/TABLE IF
# NOT EXISTS, etc.) against the DB every time it's constructed - caching per
# session means that cost is paid once per process, not once per call.
_store_cache: dict[str, object] = {}


def get_vector_store(session_id: str):
    if session_id in _store_cache:
        return _store_cache[session_id]

    logger.info(f"[STORE] Initialising vector store - session={session_id}")
    embeddings = get_embeddings()
    if not embeddings:
        logger.error("[STORE] No embedding provider available")
        raise RuntimeError("No embedding provider available. Check VOYAGE_API_KEY or OPENAI_API_KEY.")

    collection = f"session_{session_id}"

    if cfg.db_url:
        try:
            store = PGVector(
                embeddings=embeddings,
                collection_name=collection,
                connection=get_db_engine(),
            )
            logger.info(f"[STORE] Connected to pgvector - collection={collection}")
            _store_cache[session_id] = store
            return store
        except Exception as e:
            logger.warning(f"[STORE] pgvector unavailable ({e}) - falling back to ChromaDB")

    # Not cached: a transient pgvector outage must not permanently pin this
    # session to Chroma for the rest of the process's life. Leaving this
    # uncached means the next call retries pgvector fresh, instead of this
    # session's documents silently ending up split across two different
    # backing stores once the DB recovers.
    store = Chroma(
        collection_name=collection,
        embedding_function=embeddings,
        persist_directory=cfg.chroma_persist_dir,
    )
    logger.info(f"[STORE] Using ChromaDB (local) - collection={collection}")
    return store


def delete_vectors_by_source(store, filename: str) -> int:
    """Removes every chunk tagged with metadata source==filename (matched
    case-insensitively) from this session's vector store.

    Matching is case-insensitive because a host app may store filenames
    lowercased elsewhere while vector chunks keep the upload's original
    case in their "source" metadata - comparing exactly would silently
    leave orphaned chunks behind for any mixed-case filename.

    Chroma has no case-insensitive filter in its own "where" query language,
    so this fetches all chunks' metadata and filters in Python instead -
    O(n) in collection size, but this is only expected to run on an explicit
    user "detach" action, not a hot path. PGVector's public .delete() only
    accepts explicit ids (no metadata filter at all, checked against the
    installed langchain_postgres version), so this runs a raw SQL delete
    against its underlying langchain_pg_embedding/langchain_pg_collection
    tables instead - stable, documented table/column names that have been
    consistent across langchain_postgres versions, unlike undocumented
    internal attributes."""
    target = filename.lower()

    if hasattr(store, "_collection"):
        data = store._collection.get(include=["metadatas"])
        ids = [
            id_ for id_, meta in zip(data.get("ids", []), data.get("metadatas", []))
            if (meta or {}).get("source", "").lower() == target
        ]
        if ids:
            store._collection.delete(ids=ids)
        return len(ids)

    collection_name = getattr(store, "collection_name", None)
    if not collection_name:
        logger.warning(f"[STORE] delete_vectors_by_source: couldn't resolve collection name for {filename}")
        return 0

    from sqlalchemy import text
    with get_db_engine().begin() as conn:
        result = conn.execute(
            text(
                "DELETE FROM langchain_pg_embedding e "
                "USING langchain_pg_collection c "
                "WHERE e.collection_id = c.uuid AND c.name = :collection "
                "AND LOWER(e.cmetadata->>'source') = :filename"
            ),
            {"collection": collection_name, "filename": target},
        )
        return result.rowcount

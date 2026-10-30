"""Keeps the default/sample chat session (DEFAULT_SESSION_ID) in sync with
whatever files sit in a Supabase Storage bucket, so the "no documents
uploaded yet" fallback a user chats against (see app/routers/chat.py) can be
swapped by dropping files into that bucket instead of editing code.

DEFAULT_SESSION_ID ("aria_default_sample") is not a real `sessions` row -
the normal /upload/files path's dedup bookkeeping (app.services.dedup,
app.models.db.Document) requires a UUID session_id tied to one, so this
module embeds chunks straight into the vector store the same way the
original bundled-sample seeding always did, and tracks "have I already
embedded this exact file content" itself via a `content_hash` field stored
on each chunk's own metadata (read back from the store) - no extra DB table,
no filesystem sidecar, and it stays accurate even across server restarts.
"""
import asyncio
import logging

from app.config import cfg, DEFAULT_SESSION_ID

logger = logging.getLogger(__name__)

_storage_client = None


def _storage():
    global _storage_client
    if _storage_client is None:
        if not cfg.supabase_url or not cfg.supabase_service_key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env")
        from supabase import create_client
        _storage_client = create_client(cfg.supabase_url, cfg.supabase_service_key)
    return _storage_client


def _existing_source_hashes(store) -> dict[str, str]:
    """Returns {source_filename: content_hash} for every distinct source
    currently embedded in `store`, read back from chunk metadata. Mirrors
    delete_vectors_by_source's two backend-specific read paths (Chroma vs
    PGVector) in app.services.vector_store."""
    if hasattr(store, "_collection"):
        data = store._collection.get(include=["metadatas"])
        hashes: dict[str, str] = {}
        for meta in data.get("metadatas", []) or []:
            src = (meta or {}).get("source")
            h = (meta or {}).get("content_hash")
            if src and h:
                hashes[src] = h
        return hashes

    collection_name = getattr(store, "collection_name", None)
    if not collection_name:
        return {}
    from sqlalchemy import text
    from app.services.vector_store import _get_db_engine
    with _get_db_engine().begin() as conn:
        rows = conn.execute(
            text(
                "SELECT DISTINCT e.cmetadata->>'source' AS source, "
                "e.cmetadata->>'content_hash' AS content_hash "
                "FROM langchain_pg_embedding e "
                "JOIN langchain_pg_collection c ON e.collection_id = c.uuid "
                "WHERE c.name = :collection"
            ),
            {"collection": collection_name},
        ).fetchall()
    return {r.source: r.content_hash for r in rows if r.source and r.content_hash}


async def sync_default_session_from_bucket() -> int:
    """Lists every file in cfg.default_kb_bucket and embeds any that are new
    or changed into DEFAULT_SESSION_ID's vector store (re-embedding a changed
    file replaces its old chunks). Returns the number of files embedded or
    re-embedded - a file whose content hash hasn't changed since the last
    sync is skipped entirely, so calling this repeatedly/on a timer is cheap.
    """
    from app.services.vector_store import get_vector_store, delete_vectors_by_source
    from app.services.rag import chunk_text_with_offsets, find_page
    from app.services.ingestion import parse_file
    from app.services.dedup import sha256
    from app.connectors.pipeline_bridge import is_supported_extension
    from app.routers.upload import _add_texts_batched_parallel

    bucket = cfg.default_kb_bucket
    try:
        client = _storage()
        entries = await asyncio.to_thread(client.storage.from_(bucket).list)
    except Exception as e:
        logger.warning(f"[DEFAULT_KB] Could not list bucket '{bucket}': {e}")
        return 0

    # Supabase Storage represents sub-folders as entries with id=None - only
    # real files (which always have an id) are ingestion candidates.
    files = [e for e in entries if e.get("id") is not None]
    if not files:
        logger.info(f"[DEFAULT_KB] Bucket '{bucket}' is empty")
        return 0

    store = get_vector_store(DEFAULT_SESSION_ID)
    existing_hashes = _existing_source_hashes(store)

    embedded = 0
    for entry in files:
        name = entry.get("name")
        if not name or not is_supported_extension(name):
            logger.info(f"[DEFAULT_KB] Skipping '{name}' - unsupported type")
            continue
        try:
            content = await asyncio.to_thread(client.storage.from_(bucket).download, name)
        except Exception as e:
            logger.warning(f"[DEFAULT_KB] Could not download '{name}': {e}")
            continue

        content_hash = sha256(content)
        if existing_hashes.get(name) == content_hash:
            continue  # already embedded, unchanged - no re-embed

        try:
            text, page_spans = await parse_file(content, name)
            chunks_with_offsets = chunk_text_with_offsets(text)
            batch_texts, batch_metas = [], []
            for chunk, start_idx in chunks_with_offsets:
                page_number, confidence = find_page(start_idx, page_spans)
                batch_metas.append({
                    "source": name,
                    "chunk_index": len(batch_texts),
                    "page_number": page_number,
                    "confidence": confidence,
                    "content_hash": content_hash,
                })
                batch_texts.append(chunk)

            if name in existing_hashes:
                # Content changed - drop the old chunks before inserting new ones.
                delete_vectors_by_source(store, name)

            if batch_texts:
                await _add_texts_batched_parallel(store, batch_texts, batch_metas)
            logger.info(f"[DEFAULT_KB] {name} -> {len(batch_texts)} chunks")
            embedded += 1
        except Exception as e:
            logger.warning(f"[DEFAULT_KB] Failed to ingest '{name}': {e}")

    return embedded

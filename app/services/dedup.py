import asyncio
import hashlib
import logging
import math
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

from app.config import cfg

logger = logging.getLogger(__name__)

CONFIRM_TIMEOUT_S = 60


def _threshold_bytes() -> float:
    return cfg.dedup_threshold_mb * 1024 * 1024

# Confirm gate — two separate dicts so resolve() is one-shot
_pending_confirms: dict[str, asyncio.Event] = {}   # waiting for user decision
_resolved_actions: dict[str, str] = {}             # decision received, not yet consumed

# In-memory dedup cache — fallback when DB writes fail.
# (client_token, content_hash) → (session_id, chunks_stored)
_mem_cache: dict[tuple[str, str], tuple[str, int]] = {}


# ── Hashing ───────────────────────────────────────────────────────────────────

def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# ── TF-IDF similarity (no external deps) ─────────────────────────────────────

def _tfidf_similarity(text_a: str, text_b: str) -> float:
    def tokenize(t: str) -> list[str]:
        return t.lower().split()

    tokens_a = tokenize(text_a)
    tokens_b = tokenize(text_b)
    if not tokens_a or not tokens_b:
        return 0.0

    tf_a = Counter(tokens_a)
    tf_b = Counter(tokens_b)
    all_words = set(tf_a) | set(tf_b)

    # Smoothed IDF: log((N+1)/(df+1)) + 1 — prevents zero weights when all docs share a term
    N = 2
    idf = {
        w: math.log((N + 1) / ((1 if w in tf_a else 0) + (1 if w in tf_b else 0) + 1)) + 1
        for w in all_words
    }

    def vec(tf: Counter, n: int) -> dict[str, float]:
        return {w: (c / n) * idf[w] for w, c in tf.items()}

    va = vec(tf_a, len(tokens_a))
    vb = vec(tf_b, len(tokens_b))

    dot   = sum(va.get(w, 0) * vb.get(w, 0) for w in all_words)
    mag_a = math.sqrt(sum(v ** 2 for v in va.values()))
    mag_b = math.sqrt(sum(v ** 2 for v in vb.values()))

    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def _three_point_similarity(
    new_first: str, new_middle: str, new_last: str,
    stored_first: str, stored_middle: str, stored_last: str,
) -> tuple[float, float, float]:
    return (
        _tfidf_similarity(new_first,  stored_first),
        _tfidf_similarity(new_middle, stored_middle),
        _tfidf_similarity(new_last,   stored_last),
    )


def _extract_chunks(text: str) -> tuple[str, str, str]:
    n = len(text)
    first  = text[:500]
    middle = text[max(0, n // 2 - 250): n // 2 + 250]
    last   = text[max(0, n - 500):]
    return first, middle, last


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_db_session():
    try:
        from app.db.session import get_session_factory
        factory = get_session_factory()
        if factory is None:
            return None
        return factory()
    except Exception as e:
        logger.warning(f"[DEDUP] DB session unavailable: {e}")
        return None


def _find_existing(db, client_token: str, filename: str,
                   file_size: int, content_hash: str):
    from app.models.db import Document
    # Match on hash alone — SHA256 uniquely identifies content.
    # Also handles "pending" records left by prior crashed uploads.
    record = (
        db.query(Document)
        .filter(
            Document.client_token == client_token,
            Document.content_hash == content_hash,
        )
        .first()
    )
    if record is None:
        return None
    if record.status == "complete":
        return record
    # Pending: prior upload crashed before mark_complete ran.
    # If chunks were stored, promote to complete. Otherwise delete so retry works.
    if (record.chunks_stored or 0) > 0:
        record.status = "complete"
        db.commit()
        return record
    db.delete(record)
    db.commit()
    return None


def _find_tfidf_candidates(db, client_token: str):
    from app.models.db import Document
    return (
        db.query(Document)
        .filter(
            Document.client_token == client_token,
            Document.status       == "complete",
            Document.first_chunk  != None,
        )
        .order_by(Document.uploaded_at.desc())
        .limit(50)
        .all()
    )


def _insert_pending(db, client_token: str, session_id: str,
                    filename: str, file_size: int, content_hash: str,
                    first_chunk: str, middle_chunk: str, last_chunk: str,
                    avg_confidence: Optional[float], source_type: str = "file") -> bool:
    from app.models.db import Document, Session
    from sqlalchemy.exc import IntegrityError

    # documents.session_id has a FK → sessions.id, so the session row must
    # exist before we can insert the document.
    sid = uuid.UUID(session_id) if isinstance(session_id, str) else session_id
    try:
        from sqlalchemy.exc import IntegrityError as _IE
        db.add(Session(id=sid))
        db.flush()  # write session row first to satisfy FK
    except Exception as e:
        logger.warning(f"[DEDUP] Session insert failed ({type(e).__name__}: {e}) — rolling back and retrying")
        db.rollback()

    doc = Document(
        id            = uuid.uuid4(),
        session_id    = sid,
        client_token  = client_token,
        filename      = filename,
        file_size     = file_size,
        content_hash  = content_hash,
        first_chunk   = first_chunk,
        middle_chunk  = middle_chunk,
        last_chunk    = last_chunk,
        avg_confidence= str(avg_confidence) if avg_confidence is not None else None,
        source_type   = source_type,
        status        = "pending",
        uploaded_at   = datetime.now(timezone.utc),
    )
    try:
        db.add(doc)
        db.commit()
        logger.info(f"[DEDUP] Inserted pending record for {filename} (client={client_token})")
        return True
    except Exception as e:
        db.rollback()
        logger.warning(f"[DEDUP] Insert failed for {filename}: {type(e).__name__}: {e}")
        return False


def _mark_complete(db, client_token: str, content_hash: str, chunks_stored: int):
    from app.models.db import Document
    doc = (
        db.query(Document)
        .filter(
            Document.client_token == client_token,
            Document.content_hash == content_hash,
            Document.status       == "pending",
        )
        .first()
    )
    if doc:
        doc.status        = "complete"
        doc.chunks_stored = chunks_stored
        db.commit()


def _mark_failed(db, client_token: str, content_hash: str):
    from app.models.db import Document
    doc = (
        db.query(Document)
        .filter(
            Document.client_token == client_token,
            Document.content_hash == content_hash,
            Document.status       == "pending",
        )
        .first()
    )
    if doc:
        doc.status = "failed"
        db.commit()


# ── Vector collection verification ───────────────────────────────────────────

def _collection_has_vectors(session_id: str) -> bool:
    try:
        from app.services.vector_store import get_vector_store
        store = get_vector_store(session_id)
        # Chroma: check via underlying collection count
        if hasattr(store, "_collection"):
            return store._collection.count() > 0
        # pgvector: try a similarity search with a dummy query
        results = store.similarity_search("test", k=1)
        return len(results) > 0
    except Exception:
        return False


# ── Vector copy (cached session → master session) ────────────────────────────

async def copy_vectors_to_master(old_session_id: str, master_store) -> int:
    def _copy():
        try:
            from app.services.vector_store import get_vector_store
            old_store = get_vector_store(old_session_id)
            if not hasattr(old_store, "_collection"):
                return 0
            data = old_store._collection.get(
                include=["documents", "metadatas", "embeddings"]
            )
            docs = data.get("documents") or []
            if not docs:
                return 0
            new_ids = [str(uuid.uuid4()) for _ in docs]
            emb = data.get("embeddings")
            if emb:
                master_store._collection.add(
                    ids=new_ids, documents=docs,
                    metadatas=data["metadatas"], embeddings=emb,
                )
            else:
                # embeddings not returned by get() — re-add via langchain (re-embeds)
                master_store.add_texts(docs, metadatas=data["metadatas"])
            return len(docs)
        except Exception as e:
            logger.warning(f"[DEDUP] Vector copy failed: {e}")
            return 0

    return await asyncio.to_thread(_copy)


# ── Confirmation gate ─────────────────────────────────────────────────────────

def update_cache(client_token: str, content_hash: str, session_id: str, chunks_stored: int):
    """Called after a successful upload so the next upload finds a cache hit."""
    _mem_cache[(client_token, content_hash)] = (session_id, chunks_stored)
    logger.info(f"[DEDUP] Cache updated: client={client_token[:8]}… chunks={chunks_stored}")


def create_confirm_gate(confirm_token: str) -> asyncio.Event:
    event = asyncio.Event()
    _pending_confirms[confirm_token] = event
    return event


def resolve_confirm(confirm_token: str, action: str) -> bool:
    """One-shot: pops the pending entry so a second call returns False."""
    event = _pending_confirms.pop(confirm_token, None)
    if event is None:
        return False
    _resolved_actions[confirm_token] = action
    event.set()
    return True


def consume_confirm(confirm_token: str) -> str:
    return _resolved_actions.pop(confirm_token, "reprocess")


# ── Main dedup check ──────────────────────────────────────────────────────────

class DedupResult:
    def __init__(self, action: str, session_id: Optional[str] = None,
                 reason: Optional[str] = None, doc=None):
        self.action     = action      # "reuse" | "process_fresh" | "confirm"
        self.session_id = session_id  # existing session_id if reusing
        self.reason     = reason      # why we made this decision
        self.doc        = doc         # existing Document row if found


async def check(
    client_token: str,
    filename: str,
    file_size: int,
    content: bytes,
    parsed_text: str,
    avg_confidence: Optional[float],
) -> DedupResult:
    filename = filename.lower()
    content_hash = await asyncio.to_thread(sha256, content)
    threshold_bytes = _threshold_bytes()

    # Rule 0 — in-memory cache (survives DB failures, resets on server restart)
    cache_key = (client_token, content_hash)
    if cache_key in _mem_cache:
        cached_session_id, cached_chunks = _mem_cache[cache_key]
        if cached_chunks > 0:
            logger.info(f"[DEDUP] {filename} → cache hit, {cached_chunks} chunks, session={cached_session_id}")
            return DedupResult("confirm", session_id=cached_session_id,
                               reason="same_hash", doc=None)

    db = await asyncio.to_thread(_get_db_session)
    if db is None:
        logger.warning(f"[DEDUP] {filename} → DB unavailable, processing fresh")
        return DedupResult("process_fresh", reason="db_unavailable")

    try:
        # Rule 1 — hash match in DB
        existing = await asyncio.to_thread(
            _find_existing, db, client_token, filename, file_size, content_hash
        )
        if existing:
            # Use chunks_stored as a fast proxy — avoids a slow HF embedding call.
            # chunks_stored is set by mark_complete only after vectors are written.
            chunks = existing.chunks_stored or 0
            if chunks > 0:
                logger.info(f"[DEDUP] {filename} → hash match, {chunks} chunks, session={existing.session_id}")
                return DedupResult("confirm", session_id=str(existing.session_id),
                                   reason="same_hash", doc=existing)
            logger.info(f"[DEDUP] {filename} → hash match but chunks_stored=0, processing fresh")
            return DedupResult("process_fresh", reason="collection_missing")

        # Rule 2 — same filename, different hash/size → process fresh
        from app.models.db import Document as DocModel
        same_name = (
            db.query(DocModel)
            .filter(DocModel.client_token == client_token,
                    DocModel.filename == filename,
                    DocModel.status == "complete")
            .first()
        )
        if same_name:
            logger.info(f"[DEDUP] {filename} → same name, different content, processing fresh")
            return DedupResult("process_fresh", reason="same_name_different_content")

        # Rule 3 — different filename, file < threshold
        if file_size < threshold_bytes:
            logger.info(f"[DEDUP] {filename} → below {cfg.dedup_threshold_mb}MB threshold, processing fresh")
            return DedupResult("process_fresh", reason="below_threshold")

        # Rule 4 — TF-IDF 3-point check (async, CPU-bound)
        if len(parsed_text) < 1500:
            logger.info(f"[DEDUP] {filename} → text too short for TF-IDF, processing fresh")
            return DedupResult("process_fresh", reason="text_too_short")

        new_first, new_middle, new_last = _extract_chunks(parsed_text)
        candidates = await asyncio.to_thread(
            _find_tfidf_candidates, db, client_token
        )

        tfidf_threshold = 0.90 if (avg_confidence or 1.0) < 0.85 else 0.95

        def _run_tfidf():
            for candidate in candidates:
                if not all([candidate.first_chunk, candidate.middle_chunk, candidate.last_chunk]):
                    continue
                s1, s2, s3 = _three_point_similarity(
                    new_first, new_middle, new_last,
                    candidate.first_chunk, candidate.middle_chunk, candidate.last_chunk,
                )
                logger.info(
                    f"[DEDUP] {filename} → TF-IDF vs {candidate.filename}: "
                    f"scores=[{s1:.2f},{s2:.2f},{s3:.2f}] threshold={tfidf_threshold}"
                )
                if s1 > tfidf_threshold and s2 > tfidf_threshold and s3 > tfidf_threshold:
                    return candidate
            return None

        match = await asyncio.to_thread(_run_tfidf)
        if match and (match.chunks_stored or 0) > 0:
            logger.info(f"[DEDUP] {filename} → TF-IDF match, session={match.session_id}")
            return DedupResult("confirm", session_id=str(match.session_id),
                               reason="tfidf_match", doc=match)

        logger.info(f"[DEDUP] {filename} → no match, processing fresh")
        return DedupResult("process_fresh", reason="no_match")

    except Exception as e:
        logger.warning(f"[DEDUP] {filename} → error ({e}), processing fresh")
        return DedupResult("process_fresh", reason="error")
    finally:
        await asyncio.to_thread(db.close)

"""Short-TTL cache for knowledge-base lookups.

Two call sites pay a real network round trip to the database on every
single call with no caching at all:
  - app.routers.chat: looks up the selected knowledge_base_id FRESH on
    every single chat message (the worst of the two - this runs on every
    chat turn, not just a page load).
  - app.routers.knowledge_base: lists every active KB on every dropdown
    load/refresh.
In this project's environment that round trip measured ~800ms-1s against
the remote Supabase Postgres instance - genuine network latency, not an
inefficient query - so paying it per-chat-message is a serious user-facing
slowdown, not just an admin-dashboard annoyance.

Knowledge bases change rarely (an admin creates/edits one occasionally), so
a short TTL cache - invalidated immediately on every admin write via
invalidate_kb_cache(), not just left to expire - removes this round trip
from the hot path entirely without ever risking more than a few seconds of
staleness, and never anything stale right after an admin actually changes
something.

Same in-memory-per-process caching pattern as app.services.roles_service -
not safe across multiple server processes/workers without a shared cache,
consistent with this app's existing single-process assumption throughout
(see rag.py's BM25/answer caches, roles_service's role cache).
"""
import logging
import threading
import time
import uuid

from app.db.session import get_session_factory
from app.models.db import KnowledgeBase

logger = logging.getLogger(__name__)

_KB_CACHE_TTL_S = 20

_active_list_cache: list[dict] | None = None
_active_list_cache_ts: float = 0.0
_by_id_cache: dict[str, dict | None] = {}
_by_id_cache_ts: dict[str, float] = {}
_lock = threading.Lock()


def _kb_to_cache_dict(kb: KnowledgeBase) -> dict:
    return {
        "id":             str(kb.id),
        "name":           kb.name,
        "description":    kb.description,
        "status":         kb.status,
        "session_id":     kb.session_id,
        "allowed_roles":  list(kb.allowed_roles) if kb.allowed_roles else None,
    }


def invalidate_kb_cache() -> None:
    """Called by every admin.py knowledge-base create/update/delete - keeps
    the cache from ever serving stale data for longer than it takes the
    admin's own write to commit."""
    global _active_list_cache
    with _lock:
        _active_list_cache = None
        _by_id_cache.clear()
        _by_id_cache_ts.clear()


def get_active_kbs() -> list[dict]:
    """Every KB with status=="active", as plain dicts (not ORM instances -
    safe to hold onto after the session that fetched them is closed)."""
    global _active_list_cache, _active_list_cache_ts
    with _lock:
        if _active_list_cache is not None and time.monotonic() - _active_list_cache_ts < _KB_CACHE_TTL_S:
            return list(_active_list_cache)

    factory = get_session_factory()
    if factory is None:
        return []
    db = factory()
    try:
        rows = (
            db.query(KnowledgeBase)
            .filter(KnowledgeBase.status == "active")
            .order_by(KnowledgeBase.name.asc())
            .all()
        )
        result = [_kb_to_cache_dict(kb) for kb in rows]
    finally:
        db.close()

    with _lock:
        _active_list_cache = result
        _active_list_cache_ts = time.monotonic()
    return list(result)


def get_kb_by_id(kb_id: uuid.UUID | str) -> dict | None:
    """One KB by id (any status), as a plain dict, or None if it doesn't
    exist. Caches the miss too (a guessed/stale id shouldn't re-hit the DB
    on every retry within the TTL either)."""
    key = str(kb_id)
    with _lock:
        ts = _by_id_cache_ts.get(key)
        if ts is not None and time.monotonic() - ts < _KB_CACHE_TTL_S:
            cached = _by_id_cache[key]
            return dict(cached) if cached else None

    factory = get_session_factory()
    if factory is None:
        return None
    db = factory()
    try:
        kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
        result = _kb_to_cache_dict(kb) if kb else None
    finally:
        db.close()

    with _lock:
        _by_id_cache[key] = result
        _by_id_cache_ts[key] = time.monotonic()
    return dict(result) if result else None

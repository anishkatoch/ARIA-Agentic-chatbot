"""Tracks cumulative token usage per embedding model so the app can
automatically switch to the next model in the priority list once a
model's free quota is used up, or once a call against it fails outright.

Usage is cached in memory (read-through) and persisted to Postgres
(write-through) so it survives restarts; if the DB is unavailable the
app still works, it just can't remember usage across restarts.
"""
import logging
import threading

from app.config import cfg

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: dict[str, dict] = {}   # model -> {"tokens_used": int, "status": str}
_loaded = False


def _get_db_session():
    try:
        from app.db.session import get_session_factory
        factory = get_session_factory()
        if factory is None:
            return None
        return factory()
    except Exception as e:
        logger.warning(f"[EMBED-USAGE] DB session unavailable: {e}")
        return None


def _load_from_db() -> None:
    global _loaded
    db = _get_db_session()
    if db is None:
        _loaded = True  # nothing to load - proceed with in-memory defaults
        return
    try:
        from app.models.db import EmbeddingModelUsage
        rows = db.query(EmbeddingModelUsage).all()
        for row in rows:
            _cache[row.model] = {"tokens_used": row.tokens_used or 0, "status": row.status or "active"}
        _loaded = True
    except Exception as e:
        logger.warning(f"[EMBED-USAGE] Failed to load usage from DB: {e}")
        _loaded = True
    finally:
        db.close()


def _persist(model: str, provider: str) -> None:
    db = _get_db_session()
    if db is None:
        return
    try:
        from app.models.db import EmbeddingModelUsage
        row = db.get(EmbeddingModelUsage, model)
        state = _cache[model]
        if row is None:
            row = EmbeddingModelUsage(model=model, provider=provider)
            db.add(row)
        row.tokens_used = state["tokens_used"]
        row.status = state["status"]
        row.last_error = state.get("last_error")
        db.commit()
    except Exception as e:
        logger.warning(f"[EMBED-USAGE] Failed to persist usage for {model}: {e}")
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def _ensure_loaded() -> None:
    if not _loaded:
        with _lock:
            if not _loaded:
                _load_from_db()


def get_usage(model: str) -> dict:
    _ensure_loaded()
    with _lock:
        return dict(_cache.get(model, {"tokens_used": 0, "status": "active"}))


def pick_active_model(models: list[dict], provider: str = "voyage") -> str | None:
    """Return the first model (in priority order) that still has free quota
    left and hasn't been marked broken. None if every model is used up."""
    _ensure_loaded()
    with _lock:
        for entry in models:
            model = entry["model"]
            free_tokens = entry.get("free_tokens")
            state = _cache.get(model, {"tokens_used": 0, "status": "active"})
            if state["status"] in ("broken", "exhausted"):
                continue
            if free_tokens is not None and state["tokens_used"] >= free_tokens:
                continue
            return model
    return None


def record_usage(model: str, tokens: int, provider: str = "voyage") -> None:
    """Add consumed tokens to a model's running total (call after a successful embed call)."""
    _ensure_loaded()
    with _lock:
        state = _cache.setdefault(model, {"tokens_used": 0, "status": "active"})
        state["tokens_used"] += max(tokens, 0)
        state["status"] = "active"
        state["last_error"] = None
    logger.info(f"[EMBED-USAGE] {model}: +{tokens} tokens (total {_cache[model]['tokens_used']})")
    _persist(model, provider)


def mark_broken(model: str, error: str, provider: str = "voyage") -> None:
    """Mark a model as broken (call failed outright - not just quota exhaustion)
    so it's skipped on future selection without needing to keep failing."""
    _ensure_loaded()
    with _lock:
        state = _cache.setdefault(model, {"tokens_used": 0, "status": "active"})
        state["status"] = "broken"
        state["last_error"] = error[:500]
    logger.warning(f"[EMBED-USAGE] {model} marked broken: {error}")
    _persist(model, provider)


def mark_exhausted(model: str, provider: str = "voyage") -> None:
    _ensure_loaded()
    with _lock:
        state = _cache.setdefault(model, {"tokens_used": 0, "status": "active"})
        state["status"] = "exhausted"
    logger.warning(f"[EMBED-USAGE] {model} marked exhausted (free quota used up)")
    _persist(model, provider)

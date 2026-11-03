"""Tracks cumulative token usage per embedding model so this package can
automatically switch to the next model in a priority list once a model's
free quota is used up, or once a call against it fails outright.

Usage is cached in memory (read-through) and persisted to Postgres
(write-through, in its own `embedding_model_usage` table - created on first
use, no migration needed) so it survives restarts; if no DB is configured
or it's unavailable, this still works, it just can't remember usage across
restarts.
"""
import logging
import threading

from .config import cfg

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: dict[str, dict] = {}   # model -> {"tokens_used": int, "status": str}
_loaded = False
_table_ready = False


def _ensure_table(conn) -> None:
    global _table_ready
    if _table_ready:
        return
    conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS embedding_model_usage (
            model TEXT PRIMARY KEY,
            provider TEXT,
            tokens_used BIGINT DEFAULT 0,
            status TEXT DEFAULT 'active',
            last_error TEXT
        )
        """
    )
    _table_ready = True


def _get_engine():
    if not cfg.db_url:
        return None
    try:
        from .store import get_db_engine
        return get_db_engine()
    except Exception as e:
        logger.warning(f"[EMBED-USAGE] DB engine unavailable: {e}")
        return None


def _load_from_db() -> None:
    global _loaded
    engine = _get_engine()
    if engine is None:
        _loaded = True  # nothing to load - proceed with in-memory defaults
        return
    try:
        from sqlalchemy import text
        with engine.begin() as conn:
            _ensure_table(conn)
            rows = conn.execute(text("SELECT model, tokens_used, status FROM embedding_model_usage")).fetchall()
            for model, tokens_used, status in rows:
                _cache[model] = {"tokens_used": tokens_used or 0, "status": status or "active"}
        _loaded = True
    except Exception as e:
        logger.warning(f"[EMBED-USAGE] Failed to load usage from DB: {e}")
        _loaded = True


def _persist(model: str, provider: str) -> None:
    engine = _get_engine()
    if engine is None:
        return
    try:
        from sqlalchemy import text
        state = _cache[model]
        with engine.begin() as conn:
            _ensure_table(conn)
            conn.execute(
                text(
                    """
                    INSERT INTO embedding_model_usage (model, provider, tokens_used, status, last_error)
                    VALUES (:model, :provider, :tokens_used, :status, :last_error)
                    ON CONFLICT (model) DO UPDATE SET
                        provider = EXCLUDED.provider,
                        tokens_used = EXCLUDED.tokens_used,
                        status = EXCLUDED.status,
                        last_error = EXCLUDED.last_error
                    """
                ),
                {
                    "model": model,
                    "provider": provider,
                    "tokens_used": state["tokens_used"],
                    "status": state["status"],
                    "last_error": state.get("last_error"),
                },
            )
    except Exception as e:
        logger.warning(f"[EMBED-USAGE] Failed to persist usage for {model}: {e}")


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

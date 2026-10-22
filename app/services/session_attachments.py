"""Server-side record of which knowledge bases are attached to a chat
session (app.models.db.SessionKnowledgeBase) - replaces the old frontend-
only "selected knowledge base" state, so attachment survives a page refresh
(or a different browser/device hitting the same session_id) and an upload
made directly in chat never silently detaches it.

A session can have any number of knowledge bases attached at once; its own
uploads need no row here since app.routers.chat already looks those up by
session_id directly.
"""
import logging
import uuid

from app.db.session import get_session_factory
from app.models.db import Session, SessionKnowledgeBase

logger = logging.getLogger(__name__)


def _ensure_session_row(db, session_id: uuid.UUID) -> None:
    """documents.session_id and session_knowledge_bases.session_id both have
    a FK -> sessions.id, so the session row must exist first. Same lazy-
    create-on-first-use pattern as app.services.dedup._insert_pending - a
    session_id is just a UUID the frontend generates, with no separate
    "create session" endpoint anywhere in this app."""
    try:
        db.add(Session(id=session_id))
        db.flush()
    except Exception:
        db.rollback()


def attach_kb(session_id: uuid.UUID, kb_id: uuid.UUID) -> None:
    factory = get_session_factory()
    if factory is None:
        raise RuntimeError("Database unavailable")
    db = factory()
    try:
        _ensure_session_row(db, session_id)
        existing = (
            db.query(SessionKnowledgeBase)
            .filter(
                SessionKnowledgeBase.session_id == session_id,
                SessionKnowledgeBase.knowledge_base_id == kb_id,
            )
            .first()
        )
        if existing is None:
            db.add(SessionKnowledgeBase(session_id=session_id, knowledge_base_id=kb_id))
            db.commit()
            logger.info(f"[ATTACH] session={session_id} attached kb={kb_id}")
    finally:
        db.close()


def detach_kb(session_id: uuid.UUID, kb_id: uuid.UUID) -> None:
    factory = get_session_factory()
    if factory is None:
        raise RuntimeError("Database unavailable")
    db = factory()
    try:
        db.query(SessionKnowledgeBase).filter(
            SessionKnowledgeBase.session_id == session_id,
            SessionKnowledgeBase.knowledge_base_id == kb_id,
        ).delete()
        db.commit()
        logger.info(f"[ATTACH] session={session_id} detached kb={kb_id}")
    finally:
        db.close()


def list_attached_kb_ids(session_id: uuid.UUID) -> list[str]:
    """Every knowledge_base_id currently attached to this session, oldest
    first. Returns [] (not an error) if the DB is unavailable or the session
    has nothing attached - callers treat "no KBs attached" as the normal,
    common case, not a failure."""
    factory = get_session_factory()
    if factory is None:
        return []
    db = factory()
    try:
        rows = (
            db.query(SessionKnowledgeBase)
            .filter(SessionKnowledgeBase.session_id == session_id)
            .order_by(SessionKnowledgeBase.attached_at.asc())
            .all()
        )
        return [str(r.knowledge_base_id) for r in rows]
    finally:
        db.close()

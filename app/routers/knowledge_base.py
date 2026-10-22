"""User-facing read of admin-curated Knowledge Bases - lists only the
`active` ones, for the "Knowledge source" dropdown on the main chat UI.
Protected by the generic auth gate (any logged-in user), not admin-only."""
import logging

from fastapi import APIRouter, HTTPException

import uuid

from app.db.session import get_session_factory
from app.models.db import KnowledgeBase, KnowledgeBaseDocument

logger = logging.getLogger(__name__)
router = APIRouter(tags=["knowledge-bases"])


@router.get("/knowledge-bases")
async def list_active_knowledge_bases():
    factory = get_session_factory()
    if factory is None:
        return []
    db = factory()
    try:
        kbs = (
            db.query(KnowledgeBase)
            .filter(KnowledgeBase.status == "active")
            .order_by(KnowledgeBase.name.asc())
            .all()
        )
        return [
            {"id": str(kb.id), "name": kb.name, "description": kb.description}
            for kb in kbs
        ]
    finally:
        db.close()


@router.get("/knowledge-bases/{kb_id}/files")
async def list_knowledge_base_files(kb_id: uuid.UUID):
    """Any logged-in user can see which documents make up an active knowledge
    base they've attached to their chat - not just the admin who built it."""
    factory = get_session_factory()
    if factory is None:
        return []
    db = factory()
    try:
        kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id, KnowledgeBase.status == "active").first()
        if not kb:
            raise HTTPException(404, "Knowledge base not found or inactive")
        docs = (
            db.query(KnowledgeBaseDocument)
            .filter(KnowledgeBaseDocument.knowledge_base_id == kb_id, KnowledgeBaseDocument.status == "complete")
            .order_by(KnowledgeBaseDocument.uploaded_at.asc())
            .all()
        )
        return [{"filename": d.filename, "uploaded_at": d.uploaded_at.isoformat()} for d in docs]
    finally:
        db.close()

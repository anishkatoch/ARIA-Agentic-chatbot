"""User-facing read of admin-curated Knowledge Bases - lists only the
`active` ones the signed-in user's roles permit, for the "Knowledge source"
dropdown on the main chat UI. Protected by the generic auth gate (any
logged-in user), not admin-only - role-based filtering happens here, not
via a separate permission level."""
import logging

from fastapi import APIRouter, HTTPException, Request

import uuid

from app.db.session import get_session_factory
from app.models.db import KnowledgeBaseDocument
from app.services import kb_cache, roles_service, session_attachments

logger = logging.getLogger(__name__)
router = APIRouter(tags=["knowledge-bases"])


def _accessible_kb_or_404(request: Request, kb_id):
    """Looks up kb_id and 404s (never 403) if it doesn't exist, isn't
    active, or the caller's roles don't permit it - same "don't confirm a
    restricted KB with this id exists" rule as list_knowledge_base_files
    below, now reused by the attach/detach/list endpoints too."""
    user = getattr(request.state, "user", None) or {}
    user_roles = roles_service.get_user_roles(user.get("email"))
    kb = kb_cache.get_kb_by_id(kb_id)
    if not kb or kb["status"] != "active" or not roles_service.can_access_kb(user_roles, kb["allowed_roles"]):
        raise HTTPException(404, "Knowledge base not found or inactive")
    return kb


@router.get("/knowledge-bases")
async def list_active_knowledge_bases(request: Request):
    user = getattr(request.state, "user", None) or {}
    user_roles = roles_service.get_user_roles(user.get("email"))
    # Cached (app.services.kb_cache) - this list is re-fetched on every
    # sidebar open/refresh; a live ~800ms-1s DB round trip here (observed
    # against this project's remote Postgres) on every one of those made
    # the "Knowledge Base" panel feel slow to open.
    kbs = kb_cache.get_active_kbs()
    return [
        {"id": kb["id"], "name": kb["name"], "description": kb["description"]}
        for kb in kbs
        if roles_service.can_access_kb(user_roles, kb["allowed_roles"])
    ]


@router.get("/knowledge-bases/{kb_id}/files")
async def list_knowledge_base_files(request: Request, kb_id: uuid.UUID):
    """Any logged-in user can see which documents make up an active knowledge
    base they've attached to their chat - not just the admin who built it -
    as long as their roles actually permit that knowledge base."""
    user = getattr(request.state, "user", None) or {}
    user_roles = roles_service.get_user_roles(user.get("email"))
    kb = kb_cache.get_kb_by_id(kb_id)
    if not kb or kb["status"] != "active":
        raise HTTPException(404, "Knowledge base not found or inactive")
    # Treated the same as "not found" rather than 403 - doesn't confirm
    # to an unauthorized caller that a restricted KB with this id exists.
    if not roles_service.can_access_kb(user_roles, kb["allowed_roles"]):
        raise HTTPException(404, "Knowledge base not found or inactive")

    factory = get_session_factory()
    if factory is None:
        return []
    db = factory()
    try:
        # Every version of every document - older versions keep their
        # vectors and stay directly queryable, so they're listed here too,
        # not just the current one.
        docs = (
            db.query(KnowledgeBaseDocument)
            .filter(KnowledgeBaseDocument.knowledge_base_id == kb_id,
                    KnowledgeBaseDocument.status == "complete")
            .order_by(KnowledgeBaseDocument.uploaded_at.asc())
            .all()
        )
        return [{"filename": d.filename, "uploaded_at": d.uploaded_at.isoformat()} for d in docs]
    finally:
        db.close()


# ── Per-session attachment - which KBs a chat session has attached ──────────
#
# Server-side, not frontend-only: survives a page refresh (or a different
# browser/device hitting the same session_id), and uploading a new document
# directly in chat never touches this table, so it never silently detaches
# whatever was already attached.

@router.post("/sessions/{session_id}/knowledge-bases/{kb_id}")
async def attach_knowledge_base(request: Request, session_id: uuid.UUID, kb_id: uuid.UUID):
    _accessible_kb_or_404(request, kb_id)
    session_attachments.attach_kb(session_id, kb_id)
    return {"status": "attached"}


@router.delete("/sessions/{session_id}/knowledge-bases/{kb_id}")
async def detach_knowledge_base(request: Request, session_id: uuid.UUID, kb_id: uuid.UUID):
    session_attachments.detach_kb(session_id, kb_id)
    return {"status": "detached"}


@router.get("/sessions/{session_id}/knowledge-bases")
async def list_attached_knowledge_bases(request: Request, session_id: uuid.UUID):
    """Full KB objects (not just ids) - lets the frontend re-render the
    attach panel's "Active" state straight from this on page load, the same
    shape list_active_knowledge_bases already returns."""
    user = getattr(request.state, "user", None) or {}
    user_roles = roles_service.get_user_roles(user.get("email"))
    attached = []
    for kb_id in session_attachments.list_attached_kb_ids(session_id):
        kb = kb_cache.get_kb_by_id(kb_id)
        if not kb or kb["status"] != "active" or not roles_service.can_access_kb(user_roles, kb["allowed_roles"]):
            continue
        attached.append({"id": kb["id"], "name": kb["name"], "description": kb["description"]})
    return attached

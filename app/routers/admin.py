"""Admin portal API - create/manage shared Knowledge Bases.

Every route here is gated to the single fixed admin account by the
auth_middleware in app/main.py (any path under /admin/ must carry a Bearer
token whose decoded role == "admin"). _require_admin() re-checks that here
too, in case this router is ever mounted somewhere the middleware doesn't
cover.
"""
import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from app.db.session import get_session_factory
from app.models.db import KnowledgeBase, KnowledgeBaseDocument
from app.models.schemas import (
    AdminCreateUserRequest,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseUpdateRequest,
    UserRolesUpdateRequest,
)
from app.routers.upload import ALLOWED_EXTENSIONS, _add_texts_batched_parallel, sse, SSE_HEADERS
from app.services import auth_service, kb_cache, roles_service
from app.services.ingestion import parse_file
from app.services.rag import chunk_text_with_offsets, find_page
from app.services.vector_store import get_vector_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


def _require_admin(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user or user.get("role") != "admin":
        raise HTTPException(403, "Admin access required")
    return user


def _db():
    factory = get_session_factory()
    if factory is None:
        raise HTTPException(503, "Database unavailable")
    return factory()


def _kb_to_dict(kb: KnowledgeBase, document_count: int = 0, processing_count: int = 0) -> dict:
    return {
        "id":               str(kb.id),
        "name":             kb.name,
        "description":      kb.description,
        "status":           kb.status,
        "document_count":   document_count,
        "processing_count": processing_count,
        "allowed_roles":    list(kb.allowed_roles) if kb.allowed_roles else [],
        "created_at":       kb.created_at.isoformat() if kb.created_at else None,
        "updated_at":       kb.updated_at.isoformat() if kb.updated_at else None,
    }


def _counts(db, kb_id) -> tuple[int, int]:
    """(total documents, documents still pending) for a KB."""
    total = db.query(KnowledgeBaseDocument).filter(KnowledgeBaseDocument.knowledge_base_id == kb_id).count()
    pending = (
        db.query(KnowledgeBaseDocument)
        .filter(KnowledgeBaseDocument.knowledge_base_id == kb_id, KnowledgeBaseDocument.status == "pending")
        .count()
    )
    return total, pending


# ── Knowledge base CRUD ───────────────────────────────────────────────────────

@router.get("/knowledge-bases")
async def list_knowledge_bases(request: Request):
    _require_admin(request)
    db = _db()
    try:
        kbs = db.query(KnowledgeBase).order_by(KnowledgeBase.created_at.desc()).all()
        from sqlalchemy import func, case
        rows = (
            db.query(
                KnowledgeBaseDocument.knowledge_base_id,
                func.count(KnowledgeBaseDocument.id),
                func.sum(case((KnowledgeBaseDocument.status == "pending", 1), else_=0)),
            )
            .group_by(KnowledgeBaseDocument.knowledge_base_id)
            .all()
        )
        counts = {kb_id: (total, pending or 0) for kb_id, total, pending in rows}
        return [_kb_to_dict(kb, *counts.get(kb.id, (0, 0))) for kb in kbs]
    finally:
        db.close()


@router.post("/knowledge-bases")
async def create_knowledge_base(request: Request, body: KnowledgeBaseCreateRequest):
    admin = _require_admin(request)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "name is required")

    db = _db()
    try:
        existing = db.query(KnowledgeBase).filter(KnowledgeBase.name == name).first()
        if existing:
            raise HTTPException(400, f"A knowledge base named '{name}' already exists")

        kb = KnowledgeBase(
            id=uuid.uuid4(),
            name=name,
            description=body.description,
            session_id=str(uuid.uuid4()),
            status="active",
            created_by=admin.get("email"),
            allowed_roles=roles_service._normalize_roles(body.allowed_roles) or None,
        )
        db.add(kb)
        db.commit()
        db.refresh(kb)
        kb_cache.invalidate_kb_cache()
        logger.info(f"[ADMIN] Knowledge base created: {kb.name} ({kb.id})")
        return _kb_to_dict(kb, 0)
    finally:
        db.close()


@router.patch("/knowledge-bases/{kb_id}")
async def update_knowledge_base(request: Request, kb_id: uuid.UUID, body: KnowledgeBaseUpdateRequest):
    _require_admin(request)
    db = _db()
    try:
        kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
        if not kb:
            raise HTTPException(404, "Knowledge base not found")

        if body.name is not None:
            kb.name = body.name.strip()
        if body.description is not None:
            kb.description = body.description
        if body.status is not None:
            kb.status = body.status
        if body.clear_allowed_roles:
            kb.allowed_roles = None
        elif body.allowed_roles is not None:
            kb.allowed_roles = roles_service._normalize_roles(body.allowed_roles) or None
        kb.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(kb)
        kb_cache.invalidate_kb_cache()
        total, pending = _counts(db, kb.id)
        logger.info(f"[ADMIN] Knowledge base updated: {kb.name} ({kb.id}) status={kb.status}")
        return _kb_to_dict(kb, total, pending)
    finally:
        db.close()


@router.delete("/knowledge-bases/{kb_id}")
async def delete_knowledge_base(request: Request, kb_id: uuid.UUID):
    _require_admin(request)
    db = _db()
    try:
        kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
        if not kb:
            raise HTTPException(404, "Knowledge base not found")
        db.query(KnowledgeBaseDocument).filter(
            KnowledgeBaseDocument.knowledge_base_id == kb.id
        ).delete()
        db.delete(kb)
        db.commit()
        kb_cache.invalidate_kb_cache()
        logger.info(f"[ADMIN] Knowledge base deleted: {kb.name} ({kb.id})")
        return {"status": "ok"}
    finally:
        db.close()


@router.get("/knowledge-bases/{kb_id}/files")
async def list_knowledge_base_files(request: Request, kb_id: uuid.UUID):
    _require_admin(request)
    db = _db()
    try:
        kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
        if not kb:
            raise HTTPException(404, "Knowledge base not found")
        docs = (
            db.query(KnowledgeBaseDocument)
            .filter(KnowledgeBaseDocument.knowledge_base_id == kb.id)
            .order_by(KnowledgeBaseDocument.uploaded_at.desc())
            .all()
        )
        return [
            {
                "id":            str(d.id),
                "filename":      d.filename,
                "file_size":     d.file_size,
                "chunks_stored": d.chunks_stored,
                "status":        d.status,
                "uploaded_at":   d.uploaded_at.isoformat() if d.uploaded_at else None,
            }
            for d in docs
        ]
    finally:
        db.close()


@router.delete("/knowledge-bases/{kb_id}/files/{doc_id}")
async def delete_knowledge_base_file(request: Request, kb_id: uuid.UUID, doc_id: uuid.UUID):
    """Removes one file from a KB - deletes its chunks from the vector store
    (when we know their ids - only files uploaded after the vector_ids
    column was added have them) and its bookkeeping row, so an admin can
    pull a stale file and replace it with a corrected upload."""
    _require_admin(request)
    db = _db()
    try:
        doc = (
            db.query(KnowledgeBaseDocument)
            .filter(KnowledgeBaseDocument.id == doc_id, KnowledgeBaseDocument.knowledge_base_id == kb_id)
            .first()
        )
        if not doc:
            raise HTTPException(404, "File not found")
        kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
        if not kb:
            raise HTTPException(404, "Knowledge base not found")
        session_id = kb.session_id
        vector_ids = [v for v in (doc.vector_ids or "").split(",") if v]
        filename = doc.filename
        db.delete(doc)
        db.commit()
    finally:
        db.close()

    if vector_ids:
        try:
            store = get_vector_store(session_id)
            await asyncio.to_thread(store.delete, ids=vector_ids)
        except Exception as e:
            logger.warning(f"[ADMIN][KB] Failed to delete vectors for {filename} ({doc_id}): {e}")
    else:
        logger.warning(f"[ADMIN][KB] {filename} ({doc_id}) has no stored vector ids - only the record was removed")

    logger.info(f"[ADMIN] Deleted file {filename} ({doc_id}) from kb={kb_id}")
    return {"status": "ok"}


# ── File upload (bundles files into a KB's vector store) ─────────────────────

@router.post("/knowledge-bases/{kb_id}/files")
async def upload_knowledge_base_files(
    request: Request,
    kb_id: uuid.UUID,
    files: list[UploadFile] = File(...),
):
    _require_admin(request)

    db = _db()
    try:
        kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
        if not kb:
            raise HTTPException(404, "Knowledge base not found")
        session_id = kb.session_id
    finally:
        db.close()

    supported_files = []
    skipped_files: list[tuple[str, str]] = []
    for f in files:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext in ALLOWED_EXTENSIONS:
            supported_files.append(f)
        else:
            skipped_files.append((f.filename, ext))

    if not supported_files:
        raise HTTPException(
            400, f"Unsupported type for all uploaded files. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )

    file_data = [(f.filename, await f.read()) for f in supported_files]

    async def stream() -> AsyncGenerator[str, None]:
        t0 = time.time()
        store = get_vector_store(session_id)
        total_chunks = 0

        for skipped_name, skipped_ext in skipped_files:
            yield sse({
                "type":    "warning",
                "message": f"{skipped_name} wasn't processed - {skipped_ext or 'unknown type'} files aren't supported.",
            })
            await asyncio.sleep(0)

        for i, (filename, content) in enumerate(file_data):
            stage = f"process_{i}"

            # Write a "pending" row immediately - so anyone opening this KB's
            # file list (or the dashboard's processing badge) while this is
            # still running sees that it's in flight, not silently absent.
            doc_id = uuid.uuid4()
            _db1 = _db()
            try:
                _db1.add(KnowledgeBaseDocument(
                    id=doc_id, knowledge_base_id=kb_id, filename=filename,
                    file_size=len(content), status="pending",
                ))
                _db1.commit()
            finally:
                _db1.close()

            yield sse({"type": "step", "status": "start", "stage": stage,
                       "title": "Processing file", "message": f"Processing {filename}"})
            await asyncio.sleep(0)
            t1 = time.time()

            def _mark(status: str, chunks_stored: int | None = None, vector_ids: list[str] | None = None):
                _db3 = _db()
                try:
                    row = _db3.query(KnowledgeBaseDocument).filter(KnowledgeBaseDocument.id == doc_id).first()
                    if row:
                        row.status = status
                        if chunks_stored is not None:
                            row.chunks_stored = chunks_stored
                        if vector_ids is not None:
                            row.vector_ids = ",".join(vector_ids)
                        _db3.commit()
                finally:
                    _db3.close()

            try:
                text, page_spans = await parse_file(content, filename)
            except Exception as e:
                logger.error(f"[ADMIN][KB] Parse failed for {filename}: {e}")
                _mark("failed")
                yield sse({"type": "error", "stage": stage, "message": f"Failed to parse {filename}."})
                return

            chunks_with_offsets = chunk_text_with_offsets(text)
            batch_texts, batch_metas = [], []
            for chunk, start_idx in chunks_with_offsets:
                page_number, confidence = find_page(start_idx, page_spans)
                batch_metas.append({
                    "source":      filename,
                    "chunk_index": total_chunks + len(batch_texts),
                    "page_number": page_number,
                    "confidence":  confidence,
                })
                batch_texts.append(chunk)

            vector_ids: list[str] = []
            if batch_texts:
                try:
                    vector_ids = await _add_texts_batched_parallel(store, batch_texts, batch_metas)
                except Exception as e:
                    logger.error(f"[ADMIN][KB] Embedding failed for {filename}: {e}")
                    _mark("failed")
                    yield sse({"type": "error", "stage": stage,
                               "message": f"Failed to embed {filename}. Check your embedding provider and try again."})
                    return

            file_chunks   = len(batch_texts)
            total_chunks += file_chunks
            elapsed = round(time.time() - t1, 2)

            _mark("complete", file_chunks, vector_ids)

            logger.info(f"[ADMIN][KB] {filename} -> {file_chunks} chunks into kb={kb_id} session={session_id}")
            yield sse({"type": "step", "status": "done", "stage": stage,
                       "message": f"{filename} - {file_chunks} chunks stored", "elapsed": elapsed})
            await asyncio.sleep(0)

        total = round(time.time() - t0, 2)
        yield sse({"type": "complete", "files_processed": len(file_data),
                   "total_elapsed": total, "files_skipped": [name for name, _ in skipped_files],
                   "completion_message": "Knowledge base updated"})

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


# ── Users & roles ──────────────────────────────────────────────────────────────
# Regular user accounts live entirely in Supabase Auth (no local `users`
# table), so "list all users" means asking Supabase's admin API, not this
# app's own DB - role assignments (app.services.roles_service) are the only
# part of this that's actually stored locally, keyed by email.

@router.get("/users")
async def list_users(request: Request):
    """Every Supabase-registered user, merged with whatever role assignment
    (if any) exists for them locally - so a user who's never been given a
    role still shows up here with roles: []."""
    _require_admin(request)
    try:
        # to_thread: admin_list_users() is a blocking network call (cached,
        # but still synchronous on a cache miss) - running it inline here
        # would stall the whole event loop, and every other in-flight
        # request on the server, for the duration of that HTTP round-trip.
        users = await asyncio.to_thread(auth_service.admin_list_users)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        logger.error(f"[ADMIN][USERS] Failed to list Supabase users: {e}")
        raise HTTPException(502, "Failed to reach Supabase to list users.")

    role_map = roles_service.list_user_roles()
    for u in users:
        u["roles"] = role_map.get((u["email"] or "").lower(), [])
    users.sort(key=lambda u: (u["email"] or "").lower())
    return users


@router.post("/users")
async def create_user(request: Request, body: AdminCreateUserRequest):
    """Creates a pre-confirmed Supabase account directly (no OTP/self-signup
    round-trip) and assigns its initial roles in one call - the admin's
    "add a user" action."""
    _require_admin(request)
    email = body.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "A valid email is required")
    if not body.password or len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")

    try:
        created = await asyncio.to_thread(auth_service.admin_create_user, email, body.password, body.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        logger.error(f"[ADMIN][USERS] Failed to create user {email}: {e}")
        raise HTTPException(502, "Failed to create the user account.")

    roles = roles_service.set_user_roles(email, body.roles)
    logger.info(f"[ADMIN][USERS] Created {email} with roles={roles}")
    return {**created, "roles": roles}


@router.put("/users/{email}/roles")
async def update_user_roles(request: Request, email: str, body: UserRolesUpdateRequest):
    """Replaces (not merges) a user's role assignment - the admin UI always
    sends the full intended set, same pattern as PATCH on a knowledge base's
    allowed_roles."""
    _require_admin(request)
    try:
        roles = roles_service.set_user_roles(email, body.roles)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return {"email": email.strip().lower(), "roles": roles}


@router.get("/roles")
async def list_roles(request: Request):
    """Every role name already in use (by a user or a KB) - populates the
    admin UI's suggestion list, not a fixed/validated enum; a brand-new role
    name can still be typed and assigned at any time."""
    _require_admin(request)
    return roles_service.list_known_roles()

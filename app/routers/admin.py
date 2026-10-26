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

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
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
from app.services import auth_service, kb_cache, kb_versioning, roles_service
from app.services.dedup import sha256
from app.services.diff import diff_texts
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
    """(total documents, documents still pending) for a KB. Counts every
    row, including superseded versions - older versions stay fully visible
    in this KB, not hidden, so they count too."""
    total = (
        db.query(KnowledgeBaseDocument)
        .filter(KnowledgeBaseDocument.knowledge_base_id == kb_id)
        .count()
    )
    pending = (
        db.query(KnowledgeBaseDocument)
        .filter(KnowledgeBaseDocument.knowledge_base_id == kb_id,
                KnowledgeBaseDocument.status == "pending")
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
        # Every version of every document - older versions stay visible
        # here too (not hidden), since their vectors are kept and they
        # remain directly queryable in chat, not just reachable through
        # the diff/compare feature.
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
                "item_id":       d.item_id,
                "version":       d.version,
                "is_superseded": d.is_superseded,
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


def _version_to_dict(d: KnowledgeBaseDocument) -> dict:
    return {
        "id":            str(d.id),
        "filename":      d.filename,
        "item_id":       d.item_id,
        "version":       d.version,
        "is_superseded": d.is_superseded,
        "content_hash":  d.content_hash,
        "file_size":     d.file_size,
        "status":        d.status,
        "uploaded_at":   d.uploaded_at.isoformat() if d.uploaded_at else None,
    }


@router.get("/knowledge-bases/{kb_id}/documents/{filename}/versions")
async def list_document_versions(request: Request, kb_id: uuid.UUID, filename: str):
    """Version history for a filename uploaded without an item_id."""
    _require_admin(request)
    db = _db()
    try:
        docs = (
            db.query(KnowledgeBaseDocument)
            .filter(
                KnowledgeBaseDocument.knowledge_base_id == kb_id,
                KnowledgeBaseDocument.item_id.is_(None),
                KnowledgeBaseDocument.filename == filename,
            )
            .order_by(KnowledgeBaseDocument.version.asc())
            .all()
        )
        return [_version_to_dict(d) for d in docs]
    finally:
        db.close()


@router.get("/knowledge-bases/{kb_id}/items/{item_id}/versions")
async def list_item_versions(request: Request, kb_id: uuid.UUID, item_id: str):
    """Version history by item_id - returns every version even if the
    filename changed between uploads (see planning/PLAN-document-versioning.md
    "Using item_id to handle a renamed file")."""
    _require_admin(request)
    db = _db()
    try:
        docs = (
            db.query(KnowledgeBaseDocument)
            .filter(
                KnowledgeBaseDocument.knowledge_base_id == kb_id,
                KnowledgeBaseDocument.item_id == item_id,
            )
            .order_by(KnowledgeBaseDocument.version.asc())
            .all()
        )
        return [_version_to_dict(d) for d in docs]
    finally:
        db.close()


@router.get("/knowledge-bases/documents/{doc_id}/diff")
async def diff_documents(request: Request, doc_id: uuid.UUID, against: uuid.UUID):
    """Unified diff between any two document rows - two versions of the same
    item/filename, or two unrelated documents. Diffs the stored
    extracted_text (saved at upload time), not re-parsed from the file and
    never derived from embeddings."""
    _require_admin(request)
    db = _db()
    try:
        doc_a = db.query(KnowledgeBaseDocument).filter(KnowledgeBaseDocument.id == doc_id).first()
        doc_b = db.query(KnowledgeBaseDocument).filter(KnowledgeBaseDocument.id == against).first()
        if not doc_a or not doc_b:
            raise HTTPException(404, "Document not found")
        if doc_a.extracted_text is None or doc_b.extracted_text is None:
            raise HTTPException(409, "One of these documents has no stored text to diff (still processing or failed).")

        result = diff_texts(doc_a.extracted_text, doc_b.extracted_text)
        return {
            "from": _version_to_dict(doc_a),
            "to":   _version_to_dict(doc_b),
            **result,
        }
    finally:
        db.close()


# ── File upload (bundles files into a KB's vector store) ─────────────────────

@router.post("/knowledge-bases/{kb_id}/files")
async def upload_knowledge_base_files(
    request: Request,
    kb_id: uuid.UUID,
    files: list[UploadFile] = File(...),
    item_ids: list[str] | None = Form(None),
):
    """item_ids (optional): one per file, same order as `files`. When given
    for a file, it - not the filename - is the identity key used to decide
    "is this a new document or a new version of an existing one", which lets
    a renamed file still be recognized as an update (see
    planning/PLAN-document-versioning.md). Omit it (or leave that entry
    blank) to fall back to filename-based matching, same as before."""
    _require_admin(request)

    db = _db()
    try:
        kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
        if not kb:
            raise HTTPException(404, "Knowledge base not found")
        session_id = kb.session_id
    finally:
        db.close()

    # item_ids is aligned to `files` by position. If the count doesn't match
    # (client sent too few/many), ignore it entirely rather than risk
    # silently pairing the wrong item_id with the wrong file.
    if item_ids is not None and len(item_ids) != len(files):
        logger.warning(
            f"[ADMIN][KB] item_ids length ({len(item_ids)}) != files length "
            f"({len(files)}) - ignoring item_ids for this upload"
        )
        item_ids = None

    supported_files = []
    supported_item_ids: list[str | None] = []
    skipped_files: list[tuple[str, str]] = []
    for idx, f in enumerate(files):
        ext = os.path.splitext(f.filename)[1].lower()
        if ext in ALLOWED_EXTENSIONS:
            supported_files.append(f)
            raw_item_id = item_ids[idx] if item_ids else None
            supported_item_ids.append(raw_item_id.strip() if raw_item_id and raw_item_id.strip() else None)
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
            item_id = supported_item_ids[i]
            content_hash = await asyncio.to_thread(sha256, content)

            # Decide new / exact-duplicate / new-version, and (for a new
            # version) supersede the previous active row, all in one
            # transaction - see planning/PLAN-document-versioning.md
            # "Upload flow". No confirmation is asked for a version bump:
            # it proceeds automatically.
            _db1 = _db()
            try:
                decision = kb_versioning.decide_upload_action(_db1, kb_id, filename, item_id, content_hash)

                if decision.action == kb_versioning.DUPLICATE:
                    _db1.close()
                    yield sse({
                        "type": "warning",
                        "message": (
                            f"{filename} wasn't processed - this exact file is already "
                            f"here (uploaded {decision.existing.uploaded_at.isoformat() if decision.existing.uploaded_at else 'previously'})."
                        ),
                    })
                    await asyncio.sleep(0)
                    continue

                previous = decision.existing if decision.action == kb_versioning.VERSION else None
                doc = kb_versioning.start_new_version(
                    _db1, kb_id, filename, len(content), item_id, content_hash, previous=previous
                )
                doc_id = doc.id
                new_version = doc.version
                versioned_info = None
                if previous is not None:
                    versioned_info = {
                        "versioned": True,
                        "new_version": new_version,
                        "previous_version": previous.version,
                        "previous_uploaded_at": previous.uploaded_at.isoformat() if previous.uploaded_at else None,
                    }
                _db1.commit()
            finally:
                _db1.close()

            if versioned_info:
                yield sse({
                    "type": "info",
                    "message": (
                        f"{filename} - content changed since the last upload; saved as "
                        f"version {versioned_info['new_version']} "
                        f"(version {versioned_info['previous_version']} from "
                        f"{versioned_info['previous_uploaded_at']} stays in the knowledge base too)."
                    ),
                })
                await asyncio.sleep(0)

            yield sse({"type": "step", "status": "start", "stage": stage,
                       "title": "Processing file", "message": f"Processing {filename}"})
            await asyncio.sleep(0)
            t1 = time.time()

            def _mark(status: str, chunks_stored: int | None = None, vector_ids: list[str] | None = None,
                      extracted_text: str | None = None):
                _db3 = _db()
                try:
                    row = _db3.query(KnowledgeBaseDocument).filter(KnowledgeBaseDocument.id == doc_id).first()
                    if row:
                        row.status = status
                        if chunks_stored is not None:
                            row.chunks_stored = chunks_stored
                        if vector_ids is not None:
                            row.vector_ids = ",".join(vector_ids)
                        if extracted_text is not None:
                            row.extracted_text = extracted_text
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
                    "source":        filename,
                    "chunk_index":   total_chunks + len(batch_texts),
                    "page_number":   page_number,
                    "confidence":    confidence,
                    # Version identity (see planning/PLAN-document-versioning.md
                    # and app.services.rag._dedupe_to_latest_version) - lets
                    # retrieval tell two versions of the same document apart
                    # even when they share a filename, instead of silently
                    # mixing old and new content into one answer. Not
                    # including is_superseded here - it would go stale the
                    # moment a later version is uploaded, since old chunks'
                    # metadata is never touched again; the DB row's
                    # is_superseded is the single source of truth for that.
                    "document_id":   str(doc_id),
                    "item_id":       item_id,
                    "version":       new_version,
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
                    # Deliberately do NOT delete the previous version's
                    # vectors here - the old version keeps serving
                    # retrieval until a successful retry (see
                    # planning/PLAN-document-versioning.md, Phase 3 step 14).
                    return

            file_chunks   = len(batch_texts)
            total_chunks += file_chunks
            elapsed = round(time.time() - t1, 2)

            _mark("complete", file_chunks, vector_ids, extracted_text=text)

            # A superseded version's vectors are deliberately kept in the
            # store, not deleted - both the old and new version stay
            # directly queryable in chat, not just reachable through the
            # diff/compare feature. (Caveat: if two versions share the
            # exact same filename, their chunks carry identical "source"
            # metadata and become indistinguishable at retrieval time - use
            # distinct filenames per version, or item_id, to avoid that.)

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

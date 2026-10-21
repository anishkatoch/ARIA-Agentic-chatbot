"""The ONE file in app/connectors/ allowed to know about this host app's RAG
ingestion internals.

Every other module under app/connectors/ (clients/*, token_store.py,
models.py, router.py) has zero knowledge of how documents get parsed, chunked,
embedded, or deduplicated - they only deal with "fetch bytes + a filename from
some third-party source". This module is the single adapter seam between
that world and this app's RAG pipeline (app.routers.upload, app.services.dedup,
app.services.ingestion, app.services.rag, app.services.vector_store).

If you are lifting app/connectors/ into a different host application, this is
the ONLY file you need to rewrite. Everything else in the package can be
copied verbatim. Your replacement must expose an async function with this
same signature:

    async def ingest_bytes(provider: str, content: bytes, filename: str,
                            user_id: str, client_token: str) -> dict

- `provider`: the connector id the bytes came from (e.g. "github", "jira").
  Only used here as metadata tagged onto the resulting chunks/response.
- `content`: the raw file bytes (or, for record-oriented connectors like
  Jira/ServiceNow/Salesforce/Teams that render a record to plain text
  instead of exposing a raw file, the UTF-8-encoded rendered text).
- `filename`: a filename (with a real extension) for the content, used for
  extension/size validation and for display.
- `user_id`: the authenticated user id, used for connector-token lookups and
  to compute the dedup client_token.
- `client_token`: the dedup "client" key used by this app's dedup cache
  (router.py computes this as f"connector:{user_id}" and passes it through
  rather than this module re-deriving it, so pipeline_bridge stays agnostic
  about how the host app identifies a "client").

Return value (what the frontend's ingest call consumes):
    {"session_id": str, "filename": str, "chunks": int, "source": provider,
     "dedup": <optional str reason, only present on a dedup reuse/confirm>}

A different host app wiring this package in would replace the body below
with calls into its own parse -> chunk -> embed -> (optional dedup) pipeline,
keeping the same return shape so app/connectors/router.py's `/ingest` endpoint
response contract is unchanged.
"""
import logging
import os
import re
import uuid

from fastapi import HTTPException

from app.config import cfg
from app.services import dedup as dedup_svc
from app.services.ingestion import parse_file
from app.services.rag import chunk_text_with_offsets, find_page
from app.services.vector_store import get_vector_store
from app.routers.upload import ALLOWED_EXTENSIONS, _add_texts_batched_parallel

logger = logging.getLogger(__name__)


async def ingest_bytes(provider: str, content: bytes, filename: str, user_id: str, client_token: str,
                        session_id: str | None = None) -> dict:
    """Runs connector-sourced bytes through the existing RAG ingestion
    pipeline (the same parse/chunk/embed/dedup path /upload/files uses), so
    the result is a normal session_id the existing /chat endpoint can use.

    `session_id`: when the caller already has an active chat session (e.g.
    adding a second/third connector file into the same conversation), pass
    its session_id here so this document joins that session's vector store
    instead of starting an isolated new one - otherwise each ingest call
    would silently orphan every document added before it. Omit it to start
    a brand-new session (e.g. the very first document, or after "New Chat").
    Validated against `client_token` before being trusted (dedup_svc.
    resolve_session_id) - a guessed/leaked session_id from another user
    can't be used to inject a document into their session."""

    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported type: {ext}. Allowed: {', '.join(ALLOWED_EXTENSIONS)}")

    size_mb = len(content) / (1024 * 1024)
    if size_mb > cfg.max_file_size_mb:
        raise HTTPException(400, f"{filename} exceeds {cfg.max_file_size_mb} MB limit")

    session_id = dedup_svc.resolve_session_id(session_id, client_token) or str(uuid.uuid4())
    store = get_vector_store(session_id)

    text, page_spans = await parse_file(content, filename)
    avg_conf = None
    if page_spans:
        confs = [float(p["confidence"]) for p in page_spans if p.get("confidence")]
        avg_conf = sum(confs) / len(confs) if confs else None

    result = await dedup_svc.check(
        client_token=client_token,
        filename=filename,
        file_size=len(content),
        content=content,
        parsed_text=text,
        avg_confidence=avg_conf,
    )

    # dedup_svc.check() only ever returns action "process_fresh" or "confirm" -
    # "confirm" covers both an exact SHA-256 hash match (reason="same_hash",
    # zero ambiguity) and a probabilistic TF-IDF near-duplicate guess
    # (reason="tfidf_match", could be a false positive on genuinely different
    # content). /upload/files handles every "confirm" by pausing its SSE
    # stream to ask the user, defaulting to fresh reprocessing if nothing
    # confirms it in time. This path has no interactive channel to ask
    # through, so it can only safely auto-reuse the unambiguous case - an
    # exact hash match - and must fall through to fresh processing for a
    # TF-IDF guess rather than ever silently serving a different document's
    # embeddings under this filename.
    if result.action == "confirm" and result.reason == "same_hash":
        copied = await dedup_svc.copy_vectors_to_master(result.session_id, store)
        if copied == 0:
            chunks_with_offsets = chunk_text_with_offsets(text)
            batch_texts, batch_metas = [], []
            for chunk, start_idx in chunks_with_offsets:
                page_number, confidence = find_page(start_idx, page_spans)
                batch_metas.append({
                    "source": filename, "chunk_index": len(batch_texts),
                    "page_number": page_number, "confidence": confidence,
                    "connector": provider,
                })
                batch_texts.append(chunk)
            if batch_texts:
                try:
                    await _add_texts_batched_parallel(store, batch_texts, batch_metas)
                except Exception as e:
                    logger.error(f"[CONNECTORS][INGEST] {filename} -> embedding failed: {e}")
                    raise HTTPException(
                        502, f"Failed to embed {filename}. Check your embedding provider "
                             "(e.g. HuggingFace credits) and try again."
                    )
            file_chunks = len(batch_texts)
        else:
            file_chunks = copied
        dedup_svc.update_cache(client_token, dedup_svc.sha256(content), session_id, file_chunks)
        return {
            "session_id": session_id, "filename": filename, "chunks": file_chunks,
            "source": provider, "dedup": result.reason,
        }

    # Fresh - same pending/complete bookkeeping as /upload/files
    content_hash = dedup_svc.sha256(content)
    first_c, mid_c, last_c = dedup_svc._extract_chunks(text)
    db = dedup_svc._get_db_session()
    if db is not None:
        try:
            success = dedup_svc._insert_pending(
                db, client_token, session_id, filename.lower(), len(content),
                content_hash, first_c, mid_c, last_c, avg_conf, source_type="connector",
            )
            # Without this, a connector fetch slower than the periodic cleanup's
            # 5-minute threshold (large SharePoint/Drive file, slow Jira/Salesforce
            # call) would have its pending row marked "failed" by the janitor
            # while still legitimately in progress - same registry /upload/files
            # already uses to protect its own in-flight uploads.
            if success:
                dedup_svc.register_active(client_token, content_hash)
        finally:
            db.close()

    chunks_with_offsets = chunk_text_with_offsets(text)
    batch_texts, batch_metas = [], []
    for chunk, start_idx in chunks_with_offsets:
        page_number, confidence = find_page(start_idx, page_spans)
        batch_metas.append({
            "source": filename, "chunk_index": len(batch_texts),
            "page_number": page_number, "confidence": confidence,
            "connector": provider,
        })
        batch_texts.append(chunk)

    if batch_texts:
        try:
            await _add_texts_batched_parallel(store, batch_texts, batch_metas)
        except Exception as e:
            logger.error(f"[CONNECTORS][INGEST] {filename} -> embedding failed: {e}")
            db = dedup_svc._get_db_session()
            if db is not None:
                try:
                    dedup_svc._mark_failed(db, client_token, content_hash)
                finally:
                    db.close()
            dedup_svc.unregister_active(client_token, content_hash)
            raise HTTPException(
                502, f"Failed to embed {filename}. Check your embedding provider "
                     "(e.g. HuggingFace credits) and try again."
            )
    file_chunks = len(batch_texts)

    db = dedup_svc._get_db_session()
    if db is not None:
        try:
            dedup_svc._mark_complete(db, client_token, content_hash, file_chunks)
        except Exception as e:
            logger.warning(f"[CONNECTORS][INGEST] mark_complete failed for {filename}: {e}")
            try:
                dedup_svc._mark_failed(db, client_token, content_hash)
            except Exception:
                pass
        finally:
            db.close()
    dedup_svc.unregister_active(client_token, content_hash)

    dedup_svc.update_cache(client_token, content_hash, session_id, file_chunks)

    return {
        "session_id": session_id, "filename": filename, "chunks": file_chunks,
        "source": provider,
    }


async def ingest_gmail_message(message_id: str, user_id: str, client_token: str,
                                session_id: str | None = None) -> dict:
    """Ingests a whole Gmail email - its own body text plus the extracted
    text of every attachment it has - as one combined document, instead of
    requiring the caller to pick a single attachment to add. Each attachment
    goes through the same parse_file() every other document type does (so a
    PDF/DOCX/XLSX/CSV attachment's actual content is included, not just its
    filename); an attachment that fails to parse is noted and skipped rather
    than failing the whole email."""
    from app.connectors.clients.gmail import get_gmail_connector

    connector = get_gmail_connector(user_id)
    message = connector.get_message(message_id)

    parts = [
        f"Subject: {message.get('subject', '')}",
        f"From: {message.get('from', '')}",
        f"To: {message.get('to', '')}",
        f"Date: {message.get('date', '')}",
        "",
        message.get("body_text", ""),
    ]

    for att in message.get("attachments", []):
        att_filename = att.get("filename") or "attachment"
        try:
            att_content, _ = connector.download_attachment(message_id, att["attachment_id"], att_filename)
            att_text, _ = await parse_file(att_content, att_filename)
            parts.append(f"\n--- Attachment: {att_filename} ---\n{att_text}")
        except Exception as e:
            logger.warning(f"[CONNECTORS][GMAIL] Could not extract text from attachment {att_filename}: {e}")
            parts.append(f"\n--- Attachment: {att_filename} (could not extract text: {e}) ---")

    combined_text = "\n".join(parts)

    safe_subject = re.sub(r"[^A-Za-z0-9 ._-]+", "", message.get("subject", "") or "").strip()[:60] or "email"
    filename = f"{safe_subject} ({message_id[:8]}).txt"

    return await ingest_bytes(
        provider="gmail", content=combined_text.encode("utf-8"), filename=filename,
        user_id=user_id, client_token=client_token, session_id=session_id,
    )

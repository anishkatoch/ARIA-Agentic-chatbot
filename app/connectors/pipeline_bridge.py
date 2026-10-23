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

- `provider`: the connector id the bytes came from (e.g. "github", "gmail").
  Only used here as metadata tagged onto the resulting chunks/response.
- `content`: the raw file bytes (or, for record-oriented connectors like
  Teams that render a record to plain text instead of exposing a raw file,
  the UTF-8-encoded rendered text).
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

_URL_RE = re.compile(r'https?://[^\s<>\]\)"]+')


def is_supported_extension(filename: str) -> bool:
    """Whether `filename`'s extension is one ingest_bytes() will accept.
    Lets router.py pre-filter bulk-ingest candidates (e.g. a whole GitHub
    repo tree or Drive listing) down to files that won't just immediately
    400, without importing the RAG upload module directly itself."""
    return os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS


def _connector_doc_filename(prefix: str, display_name: str, item_id: str) -> str:
    """Builds the filename a conversation/space-style connector document is
    ingested under: "<prefix> - <display name> (<short id>).txt". Used by
    ingest_google_chat_space and ingest_outlook_chat so the filename carries
    the human-readable space/chat name (what a user would actually type
    when asking "what's in Rosotravel Internal Scrum"), not just the opaque
    space/chat id that caller's platform API hands back - the id suffix is
    kept only to disambiguate two spaces/chats that happen to share a name.
    """
    safe_name = re.sub(r"[^A-Za-z0-9 ._-]+", "", display_name).strip()[:60] or "chat"
    return f"{prefix} - {safe_name} ({item_id[-8:]}).txt"


async def _scrape_links_in_text(text: str, max_links: int = 3) -> str:
    """Finds plain URLs inside an email/chat-message's text and scrapes each
    one's page content through the existing scrape_url() pipeline, so a
    linked doc/page gets embedded alongside the message referencing it -
    not just the bare URL string. Capped at max_links per message to bound
    latency; a link that fails to scrape is noted inline, not fatal to the
    rest of the ingest."""
    from app.services.ingestion import scrape_url

    urls = list(dict.fromkeys(_URL_RE.findall(text or "")))[:max_links]
    if not urls:
        return ""
    parts = []
    for url in urls:
        try:
            scraped = await scrape_url(url)
            parts.append(f"\n--- Linked page: {url} ---\n{scraped}")
        except Exception as e:
            logger.warning(f"[CONNECTORS] Could not scrape linked URL {url}: {e}")
            parts.append(f"\n--- Linked page: {url} (could not fetch: {e}) ---")
    return "".join(parts)


async def ingest_bytes(provider: str, content: bytes, filename: str, user_id: str, client_token: str,
                        session_id: str | None = None, alt_client_token: str | None = None) -> dict:
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

    session_id = dedup_svc.resolve_session_id(session_id, client_token, alt_client_token) or str(uuid.uuid4())
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
            # 5-minute threshold (large SharePoint/Drive file, slow Gmail
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
                                session_id: str | None = None, alt_client_token: str | None = None) -> dict:
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

    attachment_names = [a.get("filename") or "attachment" for a in message.get("attachments", [])]
    parts = [
        f"Subject: {message.get('subject', '')}",
        f"From: {message.get('from', '')}",
        f"To: {message.get('to', '')}",
        f"Cc: {message.get('cc', '')}",
        f"Date: {message.get('date', '')}",
        f"Attachments: {', '.join(attachment_names) if attachment_names else 'None'}",
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

    link_text = await _scrape_links_in_text(message.get("body_text", ""))
    if link_text:
        parts.append(link_text)

    combined_text = "\n".join(parts)

    safe_subject = re.sub(r"[^A-Za-z0-9 ._-]+", "", message.get("subject", "") or "").strip()[:60] or "email"
    filename = f"{safe_subject} ({message_id[:8]}).txt"

    return await ingest_bytes(
        provider="gmail", content=combined_text.encode("utf-8"), filename=filename,
        user_id=user_id, client_token=client_token, session_id=session_id,
        alt_client_token=alt_client_token,
    )


async def ingest_google_chat_space(space_id: str, user_id: str, client_token: str,
                                    session_id: str | None = None, alt_client_token: str | None = None) -> dict:
    """Ingests every message in one Google Chat space/chat as one combined
    document - sender + timestamp + text for each message, plus every
    attachment's extracted text and any linked page's scraped content -
    mirroring ingest_gmail_message's "whole conversation as one document"
    approach, so the chatbot can answer who-said-what, attachment, and
    linked-page questions about a chat the same way it can for an email."""
    from app.connectors.clients.google_chat import get_google_chat_connector

    connector = get_google_chat_connector(user_id)
    messages = connector.get_all_messages(space_id)

    lines: list[str] = []
    for msg in messages:
        sender = msg.get("sender") or {}
        sender_name = sender.get("displayName") or sender.get("name", "unknown")
        created = msg.get("createTime", "")
        text = msg.get("text", "") or ""
        lines.append(f"[{created}] {sender_name}: {text}")

        for att in msg.get("attachment", []) or []:
            att_filename = att.get("contentName") or "attachment"
            resource_name = (att.get("attachmentDataRef") or {}).get("resourceName")
            if not resource_name:
                continue
            try:
                att_content, _ = connector.download_attachment(resource_name, att_filename)
                att_text, _ = await parse_file(att_content, att_filename)
                lines.append(f"\n--- Attachment: {att_filename} ---\n{att_text}")
            except Exception as e:
                logger.warning(f"[CONNECTORS][GOOGLE_CHAT] Could not extract text from attachment {att_filename}: {e}")
                lines.append(f"\n--- Attachment: {att_filename} (could not extract text: {e}) ---")

        link_text = await _scrape_links_in_text(text)
        if link_text:
            lines.append(link_text)

    combined_text = "\n".join(lines) if lines else "(empty chat/space - no messages)"

    # Use the space's human-readable display name in the filename, not the
    # opaque space_id - otherwise a question like "what's in Rosotravel
    # Internal Scrum" can never match this source by name (only a specific
    # fact from inside it, found by plain content search, would ever
    # surface it). Falls back to the id if the name lookup fails - ingestion
    # still succeeds, it just won't be name-resolvable until retried.
    try:
        display_name = connector.get_space(space_id).get("name") or space_id
    except Exception as e:
        logger.warning(f"[CONNECTORS][GOOGLE_CHAT] Could not fetch space name for {space_id}: {e}")
        display_name = space_id
    filename = _connector_doc_filename("Google Chat", display_name, space_id)

    return await ingest_bytes(
        provider="google_chat", content=combined_text.encode("utf-8"), filename=filename,
        user_id=user_id, client_token=client_token, session_id=session_id,
        alt_client_token=alt_client_token,
    )


async def ingest_outlook_message(message_id: str, user_id: str, client_token: str,
                                  session_id: str | None = None, alt_client_token: str | None = None) -> dict:
    """Ingests a whole Outlook email - body text plus every attachment's
    extracted text and any linked page's scraped content - as one combined
    document, mirroring ingest_gmail_message exactly (same header block
    shape: Subject/From/To/Cc/Date/Attachments, then the body)."""
    from app.connectors.clients.outlook import get_outlook_connector

    connector = get_outlook_connector(user_id)
    message = connector.get_message(message_id)

    attachment_names = [a.get("filename") or "attachment" for a in message.get("attachments", [])]
    parts = [
        f"Subject: {message.get('subject', '')}",
        f"From: {message.get('from', '')}",
        f"To: {message.get('to', '')}",
        f"Cc: {message.get('cc', '')}",
        f"Date: {message.get('date', '')}",
        f"Attachments: {', '.join(attachment_names) if attachment_names else 'None'}",
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
            logger.warning(f"[CONNECTORS][OUTLOOK] Could not extract text from attachment {att_filename}: {e}")
            parts.append(f"\n--- Attachment: {att_filename} (could not extract text: {e}) ---")

    link_text = await _scrape_links_in_text(message.get("body_text", ""))
    if link_text:
        parts.append(link_text)

    combined_text = "\n".join(parts)

    safe_subject = re.sub(r"[^A-Za-z0-9 ._-]+", "", message.get("subject", "") or "").strip()[:60] or "email"
    filename = f"{safe_subject} ({message_id[:8]}).txt"

    return await ingest_bytes(
        provider="outlook", content=combined_text.encode("utf-8"), filename=filename,
        user_id=user_id, client_token=client_token, session_id=session_id,
        alt_client_token=alt_client_token,
    )


async def ingest_outlook_chat(chat_id: str, user_id: str, client_token: str,
                               session_id: str | None = None, alt_client_token: str | None = None) -> dict:
    """Ingests every message in one Outlook/Teams personal chat as one
    combined document - sender + timestamp + text per message, plus any
    linked page's scraped content - mirroring ingest_google_chat_space.
    Attachments on a Teams chat message are SharePoint/OneDrive links
    (contentUrl) rather than inline bytes, so they're treated as links to
    scrape rather than downloaded directly."""
    from app.connectors.clients.outlook_chat import get_outlook_chat_connector

    connector = get_outlook_chat_connector(user_id)
    messages = connector.get_all_messages(chat_id)

    lines: list[str] = []
    for msg in messages:
        sender = ((msg.get("from") or {}).get("user") or {})
        sender_name = sender.get("displayName", "unknown")
        created = msg.get("createdDateTime", "")
        body = msg.get("body") or {}
        text = body.get("content", "") if body.get("contentType") == "text" else _strip_html_fragment(body.get("content", ""))
        lines.append(f"[{created}] {sender_name}: {text}")

        for att in msg.get("attachments", []) or []:
            att_name = att.get("name") or "attachment"
            content_url = att.get("contentUrl")
            if content_url:
                lines.append(f"\n--- Attachment: {att_name} ({content_url}) ---")

        link_text = await _scrape_links_in_text(text)
        if link_text:
            lines.append(link_text)

    combined_text = "\n".join(lines) if lines else "(empty chat - no messages)"

    # Same reasoning as ingest_google_chat_space above - use the chat's
    # human-readable name/topic, not the opaque chat_id.
    try:
        display_name = connector.get_chat(chat_id).get("name") or chat_id
    except Exception as e:
        logger.warning(f"[CONNECTORS][OUTLOOK_CHAT] Could not fetch chat name for {chat_id}: {e}")
        display_name = chat_id
    filename = _connector_doc_filename("Outlook Chat", display_name, chat_id)

    return await ingest_bytes(
        provider="outlook_chat", content=combined_text.encode("utf-8"), filename=filename,
        user_id=user_id, client_token=client_token, session_id=session_id,
        alt_client_token=alt_client_token,
    )


def _strip_html_fragment(html: str) -> str:
    """Same best-effort HTML->text fallback as outlook.py's module-level
    helper, duplicated here (not imported) to keep pipeline_bridge's only
    dependency on outlook.py limited to the lazy get_outlook_connector()
    import above, consistent with how this file treats every other client
    module as an opaque "fetch bytes" source."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html or "", flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

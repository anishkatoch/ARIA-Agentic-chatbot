import logging
import os
import uuid
from typing import Optional

import httpx
from fastapi import APIRouter, Form, Response

from app.services.ingestion import parse_file
from app.services.vector_store import get_vector_store
from app.services.rag import chunk_text, answer_question

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])

# phone number → session_id  (in-memory, cleared on server restart)
_sessions: dict[str, str] = {}

MIME_TO_EXT: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "text/plain": ".txt",
}


def _xml(message: str) -> Response:
    try:
        from twilio.twiml.messaging_response import MessagingResponse
        resp = MessagingResponse()
        resp.message(message)
        return Response(content=str(resp), media_type="application/xml")
    except ImportError:
        # Fallback if twilio not installed — still returns valid TwiML
        body = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{message}</Message></Response>'
        return Response(content=body, media_type="application/xml")


@router.post("/webhook")
async def whatsapp_webhook(
    From: str = Form(...),
    Body: str = Form(""),
    NumMedia: str = Form("0"),
    MediaUrl0: Optional[str] = Form(None),
    MediaContentType0: Optional[str] = Form(None),
):
    phone = From.replace("whatsapp:", "").strip()
    num_media = int(NumMedia)
    logger.info(f"[WHATSAPP] From={phone}, NumMedia={num_media}, Body='{Body[:80]}'")

    # ── File received ────────────────────────────────────────────
    if num_media > 0 and MediaUrl0:
        ext = MIME_TO_EXT.get(MediaContentType0 or "", "")
        if not ext:
            logger.warning(f"[WHATSAPP] Unsupported MIME: {MediaContentType0}")
            return _xml("Sorry, unsupported file type. Please send a PDF, DOC, DOCX, or TXT file.")

        # Download media from Twilio (requires Basic Auth)
        try:
            sid   = os.getenv("TWILIO_ACCOUNT_SID", "")
            token = os.getenv("TWILIO_AUTH_TOKEN", "")
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(MediaUrl0, auth=(sid, token))
                resp.raise_for_status()
                content = resp.content
            logger.info(f"[WHATSAPP] Downloaded {len(content)/1024:.1f} KB ({ext})")
        except Exception as e:
            logger.error(f"[WHATSAPP] Download failed: {e}")
            return _xml("Could not download your file. Please try again.")

        # Parse → chunk → embed → store
        try:
            filename    = f"whatsapp_upload{ext}"
            text        = await parse_file(content, filename)
            chunks      = chunk_text(text)
            session_id  = str(uuid.uuid4())
            vector_store = get_vector_store(session_id)
            vector_store.add_texts(chunks)
            _sessions[phone] = session_id
            logger.info(f"[WHATSAPP] Stored — phone={phone}, session={session_id}, chunks={len(chunks)}")
            return _xml(
                f"✅ Document ready! Processed {len(chunks)} chunks from your {ext.lstrip('.'). upper()} file.\n\n"
                "Ask me anything about it."
            )
        except Exception as e:
            logger.error(f"[WHATSAPP] Processing failed: {e}")
            return _xml(f"Failed to process your file. Error: {e}")

    # ── Text message ─────────────────────────────────────────────
    body = Body.strip()

    # No message body at all
    if not body:
        return _xml(
            "👋 Hi! Send me a document (PDF, DOC, DOCX, or TXT) "
            "and I'll answer your questions about it."
        )

    # Reset command
    if body.lower() in ("reset", "new", "start over", "restart"):
        _sessions.pop(phone, None)
        logger.info(f"[WHATSAPP] Session reset — phone={phone}")
        return _xml("Session cleared. Send me a new document to get started.")

    # No session yet
    session_id = _sessions.get(phone)
    if not session_id:
        return _xml(
            "👋 Hello! Please send me a document first (PDF, DOC, DOCX, or TXT) "
            "and I'll answer your questions about it.\n\n"
            "Tip: send *reset* at any time to start a new session."
        )

    # Answer the question
    try:
        vector_store = get_vector_store(session_id)
        answer, elapsed_ms, _ = answer_question(vector_store, body)
        logger.info(f"[WHATSAPP] Answered — phone={phone}, time={elapsed_ms}ms, q='{body[:60]}'")
        return _xml(answer)
    except Exception as e:
        logger.error(f"[WHATSAPP] Answer failed: {e}")
        return _xml("Sorry, I couldn't answer that. Please try again or send *reset* to start over.")

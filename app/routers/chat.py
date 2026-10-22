import logging
import time
from fastapi import APIRouter, HTTPException, Header, Request

from app.models.schemas import ChatRequest, ChatResponse
from app.services import kb_cache, roles_service, session_attachments
from app.services.vector_store import get_vector_store, session_has_data
from app.services.rag import answer_question
from app.config import DEFAULT_SESSION_ID

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])


def _resolve_extra_sources(session_id, user_roles: list[str]) -> list[dict]:
    """Every knowledge base currently attached to this session (via the
    attach/detach endpoints in app.routers.knowledge_base), resolved to a
    vectorstore + re-checked against the caller's CURRENT roles. A KB that
    was attached but has since gone inactive, been deleted, or the caller's
    roles no longer permit is silently skipped rather than failing the whole
    chat turn - attachment is many-to-many now, so one stale/forbidden
    attachment shouldn't block every other source still in play."""
    extra_sources = []
    for kb_id in session_attachments.list_attached_kb_ids(session_id):
        # Cached (app.services.kb_cache) - this runs on EVERY chat message,
        # per attached KB, so an uncached ~800ms-1s DB round trip here
        # (observed against this project's remote Postgres) would mean every
        # chat turn pays that tax per attachment, not just a one-off load.
        kb = kb_cache.get_kb_by_id(kb_id)
        if not kb or kb["status"] != "active":
            continue
        if not roles_service.can_access_kb(user_roles, kb["allowed_roles"]):
            continue
        extra_sources.append({
            "vectorstore": get_vector_store(kb["session_id"]),
            "session_id":  kb["session_id"],
            "label":       kb["name"],
        })
    return extra_sources


@router.post("/", response_model=ChatResponse)
async def chat(
    http_request: Request,
    request: ChatRequest,
    x_advanced_mode: str = Header(default="false"),
    x_thinking_mode: str = Header(default="false"),
):
    t0 = time.time()
    advanced  = x_advanced_mode.lower() == "true"
    thinking  = x_thinking_mode.lower() == "true"
    logger.info(
        f"[CHAT] Request - session={request.session_id}, "
        f"advanced={advanced}, thinking={thinking}, "
        f"question='{request.question[:80]}{'...' if len(request.question) > 80 else ''}'"
    )
    try:
        session_id = str(request.session_id)
        # Fall back to the sample session if the user hasn't uploaded anything yet
        if not session_has_data(session_id):
            session_id = DEFAULT_SESSION_ID
            logger.info(f"[CHAT] No data in session - using default sample session")
        vector_store = get_vector_store(session_id)

        user = getattr(http_request.state, "user", None) or {}
        user_roles = roles_service.get_user_roles(user.get("email"))
        # Attachment is keyed by the caller's OWN session_id, not the
        # possibly-defaulted sample session above - a brand new session with
        # nothing uploaded yet can still have knowledge bases attached.
        extra_sources = _resolve_extra_sources(request.session_id, user_roles)
        if extra_sources:
            logger.info(
                f"[CHAT] session={request.session_id} has {len(extra_sources)} "
                f"knowledge base(s) attached: {[s['label'] for s in extra_sources]}"
            )

        answer, elapsed_ms, citations = answer_question(
            vectorstore=vector_store,
            question=request.question,
            session_id=session_id,
            advanced=advanced,
            thinking=thinking,
            extra_sources=extra_sources,
        )
        logger.info(f"[CHAT] Done - session={request.session_id}, time={elapsed_ms}ms, citations={len(citations)}")
        return ChatResponse(
            session_id=request.session_id,
            answer=answer,
            elapsed_ms=elapsed_ms,
            citations=citations,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[CHAT] Error - session={request.session_id}, error={e}", exc_info=True)
        raise HTTPException(500, "An error occurred processing your question. Please try again.")

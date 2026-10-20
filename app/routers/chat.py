import logging
import time
from fastapi import APIRouter, HTTPException, Header

from app.models.schemas import ChatRequest, ChatResponse
from app.services.vector_store import get_vector_store, session_has_data
from app.services.rag import answer_question
from app.config import DEFAULT_SESSION_ID

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/", response_model=ChatResponse)
async def chat(
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
        answer, elapsed_ms, citations = answer_question(
            vectorstore=vector_store,
            question=request.question,
            session_id=str(request.session_id),
            advanced=advanced,
            thinking=thinking,
        )
        logger.info(f"[CHAT] Done - session={request.session_id}, time={elapsed_ms}ms, citations={len(citations)}")
        return ChatResponse(
            session_id=request.session_id,
            answer=answer,
            elapsed_ms=elapsed_ms,
            citations=citations,
        )
    except Exception as e:
        logger.error(f"[CHAT] Error - session={request.session_id}, error={e}", exc_info=True)
        raise HTTPException(500, "An error occurred processing your question. Please try again.")

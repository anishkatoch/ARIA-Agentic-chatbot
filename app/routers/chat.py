import logging
import time
from fastapi import APIRouter, HTTPException

from app.models.schemas import ChatRequest, ChatResponse
from app.services.vector_store import get_vector_store
from app.services.rag import answer_question

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/", response_model=ChatResponse)
async def chat(request: ChatRequest):
    t0 = time.time()
    logger.info(f"[CHAT] Request — session={request.session_id}, question='{request.question[:80]}{'...' if len(request.question)>80 else ''}'")
    try:
        vector_store = get_vector_store(str(request.session_id))
        answer, elapsed_ms, citations = answer_question(vector_store, request.question)
        logger.info(f"[CHAT] Done — session={request.session_id}, time={elapsed_ms}ms, citations={len(citations)}")
        return ChatResponse(session_id=request.session_id, answer=answer, elapsed_ms=elapsed_ms, citations=citations)
    except Exception as e:
        logger.error(f"[CHAT] Error — session={request.session_id}, error={e}", exc_info=True)
        raise HTTPException(500, f"Error generating response: {e}")

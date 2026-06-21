import logging
import time
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TypedDict, Optional

from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.messages import HumanMessage
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langgraph.graph import StateGraph, END

from app.config import cfg

logger = logging.getLogger(__name__)

# ── Conversation history ──────────────────────────────────────────────────────
_history_store: dict[str, list[dict]] = {}  # session_id → [{"role": "user"|"assistant", "content": "..."}]
_history_lock  = threading.Lock()
_HISTORY_MAX_TURNS = 10  # keep last N user+assistant pairs per session


def _history_get(session_id: str) -> list[dict]:
    with _history_lock:
        return list(_history_store.get(session_id, []))


def _history_append(session_id: str, role: str, content: str):
    with _history_lock:
        turns = _history_store.setdefault(session_id, [])
        turns.append({"role": role, "content": content})
        # Keep only last N pairs (each pair = 2 entries)
        if len(turns) > _HISTORY_MAX_TURNS * 2:
            _history_store[session_id] = turns[-(  _HISTORY_MAX_TURNS * 2):]


def _history_to_str(history: list[dict]) -> str:
    """Format history as a readable string for prompt injection."""
    if not history:
        return ""
    lines = []
    for turn in history:
        prefix = "User" if turn["role"] == "user" else "Assistant"
        lines.append(f"{prefix}: {turn['content']}")
    return "\n".join(lines)


# ── Answer cache (Option 4) ───────────────────────────────────────────────────
_answer_cache: dict = {}
_cache_lock = threading.Lock()
_CACHE_MAX  = 256  # max entries before evicting oldest


def _cache_key(session_id: str, question: str, advanced: bool) -> str:
    raw = f"{session_id}|{question.strip().lower()}|{advanced}"
    return hashlib.md5(raw.encode()).hexdigest()


def _cache_get(key: str):
    with _cache_lock:
        return _answer_cache.get(key)


def _cache_put(key: str, value):
    with _cache_lock:
        if len(_answer_cache) >= _CACHE_MAX:
            # evict oldest entry
            oldest = next(iter(_answer_cache))
            del _answer_cache[oldest]
        _answer_cache[key] = value


# ── LLM ───────────────────────────────────────────────────────────────────────

def _is_rate_limit(e: Exception) -> bool:
    err = str(e).lower()
    return "429" in err or "rate limit" in err or "rate_limit" in err or "too many requests" in err


def _openai_llm():
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=cfg.openai_llm_model,
        openai_api_key=cfg.openai_api_key,
        temperature=0.2,
    )


def get_llm():
    return ChatGroq(
        model=cfg.groq_model,
        groq_api_key=cfg.groq_api_key,
        temperature=0.2,
        max_retries=0,
    )


def _groq_llm(model: str):
    return ChatGroq(model=model, groq_api_key=cfg.groq_api_key, temperature=0.2, max_retries=0)


def _all_groq_models() -> list[str]:
    """Primary model first, then fallbacks (deduped, preserving order)."""
    seen, result = set(), []
    for m in [cfg.groq_model] + list(cfg.groq_fallback_models):
        if m not in seen:
            seen.add(m)
            result.append(m)
    return result


def llm_invoke(messages: list) -> object:
    """Try each Groq model in order, then OpenAI if all are rate-limited and fallback enabled."""
    last_exc = None
    for model in _all_groq_models():
        try:
            result = _groq_llm(model).invoke(messages)
            if model != cfg.groq_model:
                logger.info(f"[LLM] Using Groq fallback model: {model}")
            return result
        except Exception as e:
            if _is_rate_limit(e):
                logger.warning(f"[LLM] Groq model {model} rate-limited, trying next")
                last_exc = e
            else:
                raise
    if cfg.openai_fallback_enabled and cfg.openai_api_key:
        logger.warning(f"[LLM] All Groq models exhausted — falling back to OpenAI {cfg.openai_llm_model}")
        return _openai_llm().invoke(messages)
    raise last_exc


def chain_invoke(prompt, input_dict: dict) -> str:
    """Try each Groq model in order, then OpenAI if all are rate-limited and fallback enabled."""
    last_exc = None
    for model in _all_groq_models():
        try:
            chain = prompt | _groq_llm(model) | StrOutputParser()
            result = chain.invoke(input_dict)
            if model != cfg.groq_model:
                logger.info(f"[LLM] Using Groq fallback model: {model}")
            return result
        except Exception as e:
            if _is_rate_limit(e):
                logger.warning(f"[LLM] Groq model {model} rate-limited, trying next")
                last_exc = e
            else:
                raise
    if cfg.openai_fallback_enabled and cfg.openai_api_key:
        logger.warning(f"[LLM] All Groq models exhausted — falling back to OpenAI {cfg.openai_llm_model}")
        chain = prompt | _openai_llm() | StrOutputParser()
        return chain.invoke(input_dict)
    raise last_exc


# ── Chunking helpers (used by upload pipeline) ────────────────────────────────

def chunk_text(text: str) -> list[str]:
    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
    chunks = splitter.split_text(text)
    logger.debug(f"[CHUNK] Split into {len(chunks)} chunks (size=1000, overlap=200)")
    return chunks


def chunk_text_with_offsets(text: str) -> list[tuple[str, int]]:
    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200, add_start_index=True)
    docs = splitter.create_documents([text])
    return [(doc.page_content, doc.metadata["start_index"]) for doc in docs]


def find_page(char_offset: int, page_spans: list[dict]) -> tuple[int, float | None]:
    for span in page_spans:
        if span["start"] <= char_offset < span["end"]:
            return span["page_number"], span["confidence"]
    last = page_spans[-1]
    return last["page_number"], last["confidence"]


# ── BM25 index (per session, rebuilt on demand) ───────────────────────────────

_bm25_cache: dict[str, tuple] = {}  # session_id → (BM25Okapi, docs)


def _get_or_build_bm25(vectorstore, session_id: str):
    if session_id in _bm25_cache:
        return _bm25_cache[session_id]
    try:
        from rank_bm25 import BM25Okapi
        if hasattr(vectorstore, "_collection"):
            data = vectorstore._collection.get(include=["documents", "metadatas"])
        else:
            data = {"documents": [], "metadatas": []}

        docs = data.get("documents") or []
        metas = data.get("metadatas") or []
        if not docs:
            logger.warning(f"[BM25] No documents found for session={session_id}, skipping BM25")
            return None, []

        tokenized = [d.lower().split() for d in docs]
        bm25 = BM25Okapi(tokenized)
        _bm25_cache[session_id] = (bm25, docs, metas)
        logger.info(f"[BM25] Built index for session={session_id} — {len(docs)} docs")
        return bm25, docs, metas
    except Exception as e:
        logger.warning(f"[BM25] index build failed for session={session_id}: {e}")
        return None, [], []


def _bm25_search(vectorstore, session_id: str, query: str, top_k: int) -> list:
    try:
        result = _get_or_build_bm25(vectorstore, session_id)
        if result[0] is None:
            return []
        bm25, docs, metas = result
        scores = bm25.get_scores(query.lower().split())
        top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]
        from langchain_core.documents import Document
        return [
            Document(page_content=docs[i], metadata=metas[i] if i < len(metas) else {})
            for i in top_indices if scores[i] > 0
        ]
    except Exception as e:
        logger.warning(f"[BM25] search failed: {e}")
        return []


# ── Reranker ──────────────────────────────────────────────────────────────────

def _rerank(question: str, docs: list, top_n: int) -> list:
    if not docs:
        return docs
    try:
        from huggingface_hub import InferenceClient
        client = InferenceClient(token=cfg.hf_api_key)
        passages = [doc.page_content for doc in docs]
        # sentence_similarity returns a list of float scores, one per passage
        scores = client.sentence_similarity(
            sentence=question,
            other_sentences=passages,
            model=cfg.hf_model_id,  # use same BGE embedding model (free, available)
        )
        scored = list(zip(docs, scores))
        scored.sort(key=lambda x: x[1], reverse=True)
        reranked = [d for d, _ in scored[:top_n]]
        logger.info(f"[RERANK] {len(docs)} → top {len(reranked)} after reranking (sentence_similarity)")
        return reranked
    except Exception as e:
        logger.warning(f"[RERANK] Reranker unavailable ({e}), using RRF top {top_n} directly")
        return docs[:top_n]


# ── RRF merge ─────────────────────────────────────────────────────────────────

def _rrf_merge(lists: list[list], top_k: int, k: int = 60) -> list:
    scores: dict[str, float] = {}
    doc_map: dict[str, object] = {}
    for ranked_list in lists:
        for rank, doc in enumerate(ranked_list):
            key = doc.page_content[:100]
            scores[key] = scores.get(key, 0) + 1 / (rank + k + 1)
            doc_map[key] = doc
    sorted_keys = sorted(scores, key=lambda k: scores[k], reverse=True)
    return [doc_map[k] for k in sorted_keys[:top_k]]


# ── MMR retrieval ─────────────────────────────────────────────────────────────

def _mmr_search(vectorstore, query: str, top_k: int) -> list:
    try:
        retriever = vectorstore.as_retriever(
            search_type="mmr",
            search_kwargs={
                "k":           top_k,
                "fetch_k":     max(top_k * 2, cfg.retrieval_fetch_k),
                "lambda_mult": cfg.retrieval_lambda,
            },
        )
        return retriever.invoke(query)
    except Exception as e:
        logger.warning(f"[MMR] search failed for query='{query[:60]}': {e}")
        return []


# ── Hybrid retrieval (BM25 + MMR → RRF → Rerank) ─────────────────────────────

def _hybrid_retrieve(vectorstore, session_id: str, queries: list[str]) -> list:
    top_k = cfg.hybrid_top_k
    all_lists = []
    for q in queries:
        mmr_docs  = _mmr_search(vectorstore, q, top_k)
        bm25_docs = _bm25_search(vectorstore, session_id, q, top_k)
        if mmr_docs:
            all_lists.append(mmr_docs)
        if bm25_docs:
            all_lists.append(bm25_docs)

    if not all_lists:
        logger.warning("[HYBRID] No results from MMR or BM25")
        return []

    merged = _rrf_merge(all_lists, top_k=top_k)
    reranked = _rerank(queries[0], merged, cfg.rerank_top_n)
    logger.info(f"[HYBRID] queries={len(queries)}, merged={len(merged)}, final={len(reranked)}")
    return reranked


# ── Intent check ─────────────────────────────────────────────────────────────

def check_intent(question: str, history: list[dict] | None = None) -> bool:
    try:
        _intent_llm = ChatGroq(
            model=cfg.groq_intent_model,
            groq_api_key=cfg.groq_api_key,
            temperature=0.0,
            max_retries=0,
        )
        history_block = ""
        if history:
            history_block = f"Recent conversation:\n{_history_to_str(history)}\n\n"
        prompt = (
            "A user is talking to an AI that has access to documents they uploaded.\n"
            "Your job: decide if the LATEST message (considering conversation context) "
            "contains ANY request for document information.\n\n"
            f"{history_block}"
            "Answer NO ONLY if the latest message is pure small talk with no document request — "
            "even considering context. Examples of NO: 'hi', 'thanks', 'ok', 'got it', 'bye'.\n\n"
            "Answer YES if the latest message — in context — implies a document question. "
            "Example: if the AI just asked 'Do you want me to explain clause 3?' and the user says "
            "'yup' or 'yes please' → that is YES (document needed).\n\n"
            "Rule: if in doubt, answer YES.\n\n"
            "Answer YES or NO only — no explanation.\n"
            f"Latest message: {question}"
        )
        response = _intent_llm.invoke([HumanMessage(content=prompt)])
        answer = response.content.strip().upper()
        needs_retrieval = answer.startswith("YES")
        logger.info(f"[INTENT] question='{question[:80]}' → needs_retrieval={needs_retrieval}")
        return needs_retrieval
    except Exception as e:
        logger.warning(f"[INTENT] check failed ({e}), defaulting to retrieval=True")
        return True


# ── LangGraph state ───────────────────────────────────────────────────────────

class RAGState(TypedDict):
    question:       str
    session_id:     str
    vectorstore:    object
    queries:        list[str]
    vector_docs:    list
    graph_context:  str
    answer:         str
    citations:      list[dict]
    fallback:       bool
    history:        list[dict]  # conversation turns for this session


# ── Simple pipeline nodes (HyDE → Hybrid → LLM) ──────────────────────────────

def _run_hyde(question: str) -> list[str]:
    """Generate HyDE query — can be called standalone for parallel execution."""
    try:
        prompt = (
            "Write a short hypothetical answer (2-3 sentences) to the following question "
            "as if you were reading it from a document. Do not say you don't know.\n"
            f"Question: {question}"
        )
        response = llm_invoke([HumanMessage(content=prompt)])
        hyde_query = response.content.strip()
        logger.info(f"[HYDE] Generated: '{hyde_query[:100]}'")
        return [hyde_query, question]
    except Exception as e:
        logger.warning(f"[HYDE] Failed ({e}), using original question")
        return [question]


def node_hyde(state: RAGState) -> RAGState:
    state["queries"] = _run_hyde(state["question"])
    return state


def node_simple_retrieve(state: RAGState) -> RAGState:
    docs = _hybrid_retrieve(state["vectorstore"], state["session_id"], state["queries"])
    state["vector_docs"]   = docs
    state["graph_context"] = ""
    return state


def node_simple_answer(state: RAGState) -> RAGState:
    docs     = state["vector_docs"]
    question = state["question"]
    context  = "\n\n".join(doc.page_content for doc in docs)
    history  = state.get("history") or []
    history_block = f"Conversation so far:\n{_history_to_str(history)}\n\n" if history else ""

    prompt = ChatPromptTemplate.from_template(
        "You are a helpful assistant analyzing uploaded documents.\n\n"
        "{history_block}"
        "Context from documents:\n{context}\n\n"
        "Answer the user's latest message based on the context and conversation above.\n"
        "- If asked to summarize → summarize everything in the context\n"
        "- If asked something specific → answer from context\n"
        "- If asked for opinion/advice → reason from context and give your view\n"
        "- If context has no relevant info → say so clearly\n\n"
        "Latest message: {question}"
    )
    answer = chain_invoke(prompt, {"history_block": history_block, "context": context, "question": question})
    logger.info(f"[LLM][SIMPLE] answer_length={len(answer)}")

    state["answer"]    = answer
    state["citations"] = _build_citations(docs)
    return state


# ── Advanced pipeline nodes (QueryRewrite → Hybrid + Neo4j → LLM) ────────────

def node_query_rewrite(state: RAGState) -> RAGState:
    question = state["question"]
    try:
        prompt = (
            "Rewrite the following question into 3 different search queries to find "
            "relevant chunks in a document. Return only the queries, one per line, no numbering.\n"
            f"Question: {question}"
        )
        response = llm_invoke([HumanMessage(content=prompt)])
        queries = [q.strip() for q in response.content.strip().split("\n") if q.strip()][:3]
        if not queries:
            queries = [question]
        logger.info(f"[REWRITE] {len(queries)} query variants generated")
        state["queries"] = queries
    except Exception as e:
        logger.warning(f"[REWRITE] Query rewriting failed ({e}), using original question")
        state["queries"] = [question]
    return state


def node_advanced_retrieve(state: RAGState) -> RAGState:
    docs = _hybrid_retrieve(state["vectorstore"], state["session_id"], state["queries"])
    state["vector_docs"] = docs

    # Graph retrieval
    try:
        from app.services.graph_store import query_graph
        graph_ctx = query_graph(state["question"], state["session_id"])
        state["graph_context"] = graph_ctx
        logger.info(f"[GRAPH] Retrieved graph context length={len(graph_ctx)}")
    except Exception as e:
        logger.warning(f"[ADVANCED] Neo4j graph retrieval failed ({e}) — using vector only")
        state["graph_context"] = ""

    return state


def node_advanced_answer(state: RAGState) -> RAGState:
    docs         = state["vector_docs"]
    question     = state["question"]
    vector_ctx   = "\n\n".join(doc.page_content for doc in docs)
    graph_ctx    = state.get("graph_context", "")
    fallback_msg = "\n\n⚠ Advanced mode unavailable, using standard mode." if state.get("fallback") else ""
    history      = state.get("history") or []
    history_block = f"Conversation so far:\n{_history_to_str(history)}\n\n" if history else ""

    graph_section = (
        f"\nRelated entities and relationships:\n{graph_ctx}\n"
        if graph_ctx else ""
    )

    prompt = ChatPromptTemplate.from_template(
        "You are a helpful assistant analyzing uploaded documents.\n\n"
        "{history_block}"
        "Context (from documents):\n{vector_ctx}\n"
        "{graph_section}"
        "Answer the user's latest message based on the context and conversation above.\n"
        "- If asked to summarize → summarize everything in the context\n"
        "- If asked something specific → answer from context\n"
        "- If asked for opinion/advice → reason from context and give your view\n"
        "- If context has no relevant info → say so clearly\n\n"
        "Latest message: {question}"
    )
    answer = chain_invoke(prompt, {
        "history_block": history_block,
        "vector_ctx":    vector_ctx,
        "graph_section": graph_section,
        "question":      question,
    })
    logger.info(f"[LLM][ADVANCED] answer_length={len(answer)}, graph_used={bool(graph_ctx)}")

    state["answer"]    = answer + fallback_msg
    state["citations"] = _build_citations(docs)
    return state


# ── Direct answer (small talk) ────────────────────────────────────────────────

def _direct_answer(question: str, history: list[dict] | None = None) -> tuple[str, int, list[dict]]:
    t0 = time.time()
    try:
        history_block = f"Conversation so far:\n{_history_to_str(history)}\n\n" if history else ""
        prompt = (
            "You are a helpful and friendly assistant.\n"
            f"{history_block}"
            f"Respond naturally to the following message.\n"
            f"Message: {question}"
        )
        response = llm_invoke([HumanMessage(content=prompt)])
        answer = response.content.strip()
    except Exception as e:
        logger.warning(f"[DIRECT] LLM call failed: {e}")
        answer = "Hello! How can I help you?"
    elapsed_ms = int((time.time() - t0) * 1000)
    logger.info(f"[DIRECT] small talk answered in {elapsed_ms}ms")
    return answer, elapsed_ms, []


# ── Build citations ───────────────────────────────────────────────────────────

def _build_citations(docs: list) -> list[dict]:
    citations = []
    for i, doc in enumerate(docs):
        citations.append({
            "source":      doc.metadata.get("source", "unknown"),
            "chunk_index": doc.metadata.get("chunk_index", i),
            "page_number": doc.metadata.get("page_number"),
            "confidence":  doc.metadata.get("confidence"),
            "preview":     doc.page_content[:150].strip(),
        })
        logger.debug(f"[CITE] source={citations[-1]['source']}, chunk={citations[-1]['chunk_index']}")
    return citations


# ── Build LangGraph pipelines ─────────────────────────────────────────────────

def _build_simple_graph():
    g = StateGraph(RAGState)
    g.add_node("hyde",     node_hyde)
    g.add_node("retrieve", node_simple_retrieve)
    g.add_node("answer",   node_simple_answer)
    g.set_entry_point("hyde")
    g.add_edge("hyde",     "retrieve")
    g.add_edge("retrieve", "answer")
    g.add_edge("answer",   END)
    return g.compile()


def _build_advanced_graph():
    g = StateGraph(RAGState)
    g.add_node("rewrite",  node_query_rewrite)
    g.add_node("retrieve", node_advanced_retrieve)
    g.add_node("answer",   node_advanced_answer)
    g.set_entry_point("rewrite")
    g.add_edge("rewrite",  "retrieve")
    g.add_edge("retrieve", "answer")
    g.add_edge("answer",   END)
    return g.compile()


def _build_simple_graph_no_hyde():
    """Simple graph starting at retrieve — used when HyDE already ran in parallel."""
    g = StateGraph(RAGState)
    g.add_node("retrieve", node_simple_retrieve)
    g.add_node("answer",   node_simple_answer)
    g.set_entry_point("retrieve")
    g.add_edge("retrieve", "answer")
    g.add_edge("answer",   END)
    return g.compile()


_simple_graph         = None
_simple_graph_no_hyde = None
_advanced_graph       = None


def _get_simple_graph():
    global _simple_graph
    if _simple_graph is None:
        _simple_graph = _build_simple_graph()
    return _simple_graph


def _get_simple_graph_no_hyde():
    global _simple_graph_no_hyde
    if _simple_graph_no_hyde is None:
        _simple_graph_no_hyde = _build_simple_graph_no_hyde()
    return _simple_graph_no_hyde


def _get_advanced_graph():
    global _advanced_graph
    if _advanced_graph is None:
        _advanced_graph = _build_advanced_graph()
    return _advanced_graph


# ── Main entry point ──────────────────────────────────────────────────────────

def answer_question(
    vectorstore,
    question: str,
    session_id: str = "default",
    advanced: bool = False,
) -> tuple[str, int, list[dict]]:
    t0 = time.time()
    logger.info(f"[CHAT] question='{question[:100]}', session={session_id}, advanced={advanced}")

    # ── Option 4: Cache check ──────────────────────────────────────
    cache_key = _cache_key(session_id, question, advanced)
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.info(f"[CACHE] Hit — returning cached answer in <1ms")
        return cached

    # Load conversation history for this session
    history = _history_get(session_id)

    # ── Option 1: Intent + HyDE in parallel (simple mode only) ────
    if not advanced:
        with ThreadPoolExecutor(max_workers=2) as ex:
            intent_future = ex.submit(check_intent, question, history)
            hyde_future   = ex.submit(_run_hyde, question)

            needs_retrieval = intent_future.result()
            if not needs_retrieval:
                hyde_future.cancel()
                answer, elapsed_ms, citations = _direct_answer(question, history)
                _history_append(session_id, "user", question)
                _history_append(session_id, "assistant", answer)
                return answer, elapsed_ms, citations

            hyde_queries = hyde_future.result()

        logger.info("[CHAT] Running simple pipeline (HyDE + BM25 + MMR + Rerank)")
        initial_state: RAGState = {
            "question":      question,
            "session_id":    session_id,
            "vectorstore":   vectorstore,
            "queries":       hyde_queries,
            "vector_docs":   [],
            "graph_context": "",
            "answer":        "",
            "citations":     [],
            "fallback":      False,
            "history":       history,
        }
        try:
            final_state = _get_simple_graph_no_hyde().invoke(initial_state)
        except Exception as e:
            logger.error(f"[SIMPLE] Pipeline failed: {e}")
            elapsed_ms = int((time.time() - t0) * 1000)
            return "Sorry, I encountered an error processing your question. Please try again.", elapsed_ms, []

    else:
        # ── Option 3: Advanced mode — query rewrite only, no HyDE ─
        needs_retrieval = check_intent(question, history)
        if not needs_retrieval:
            answer, elapsed_ms, citations = _direct_answer(question, history)
            _history_append(session_id, "user", question)
            _history_append(session_id, "assistant", answer)
            return answer, elapsed_ms, citations

        logger.info("[CHAT] Running advanced pipeline (QueryRewrite + BM25 + MMR + Neo4j + Rerank)")
        initial_state: RAGState = {
            "question":      question,
            "session_id":    session_id,
            "vectorstore":   vectorstore,
            "queries":       [],
            "vector_docs":   [],
            "graph_context": "",
            "answer":        "",
            "citations":     [],
            "fallback":      False,
            "history":       history,
        }
        try:
            final_state = _get_advanced_graph().invoke(initial_state)
        except Exception as e:
            logger.error(f"[ADVANCED] Pipeline failed ({e}) — falling back to simple pipeline")
            initial_state["fallback"] = True
            try:
                final_state = _get_simple_graph_no_hyde().invoke(initial_state)
            except Exception as e2:
                logger.error(f"[SIMPLE] Fallback pipeline also failed: {e2}")
                elapsed_ms = int((time.time() - t0) * 1000)
                return "Sorry, I encountered an error processing your question. Please try again.", elapsed_ms, []

    elapsed_ms = int((time.time() - t0) * 1000)
    logger.info(
        f"[CHAT] Done — citations={len(final_state['citations'])}, "
        f"fallback={final_state.get('fallback', False)}, total_time={elapsed_ms}ms"
    )
    # Save turns to history
    _history_append(session_id, "user", question)
    _history_append(session_id, "assistant", final_state["answer"])

    result = (final_state["answer"], elapsed_ms, final_state["citations"])
    _cache_put(cache_key, result)
    return result

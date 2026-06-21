"""
End-to-end RAG pipeline test with LLM-as-Judge scoring.

Runs real calls (Groq LLM + ChromaDB) against a known test corpus,
then asks a second LLM call to score each answer on:
  - faithfulness   (is the answer grounded in the context?)
  - completeness   (does it cover what was asked?)
  - no_hallucination (does it avoid inventing facts?)

Each criterion is scored 1-5. A score < 3 is flagged as a bug.

Run with:
    uv run pytest tests/test_e2e_llm_judge.py -v -s
"""

import time
import json
import logging
import pytest
from langchain_core.messages import HumanMessage
from langchain_core.documents import Document

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Sample corpus (embedded inline — no file upload needed) ──────────────────

CORPUS = """
MUTUAL NON-DISCLOSURE AGREEMENT

This Mutual Non-Disclosure Agreement ("Agreement") is entered into as of January 15, 2025,
by and between Acme Corp, a Delaware corporation ("Company A"), and Beta Solutions Ltd,
a UK company ("Company B"). Both parties wish to explore a potential business partnership
and may need to share proprietary and confidential information with each other.

1. DEFINITION OF CONFIDENTIAL INFORMATION
   "Confidential Information" means any non-public information disclosed by one party
   to the other, whether orally, in writing, or by any other means, including but not
   limited to trade secrets, business plans, financial data, customer lists, pricing
   strategies, technical specifications, source code, and proprietary algorithms.
   Confidential Information also includes any information that is marked as confidential
   or that a reasonable person would understand to be confidential given the nature of
   the information and circumstances of disclosure.

2. OBLIGATIONS OF EACH PARTY
   Each receiving party agrees to: (a) hold the disclosing party's Confidential
   Information in strict confidence using at least the same degree of care it uses to
   protect its own confidential information, but in no event less than reasonable care;
   (b) not disclose it to any third party without prior written consent from the
   disclosing party; (c) use it solely for the purpose of evaluating a potential
   business partnership between the parties; (d) restrict access to Confidential
   Information to employees or contractors who have a need to know and who are bound
   by confidentiality obligations at least as protective as those in this Agreement.

3. TERM AND DURATION
   This Agreement shall remain in effect for a period of three (3) years from the
   effective date of January 15, 2025, unless terminated earlier by mutual written
   agreement of both parties. Obligations with respect to Confidential Information
   disclosed during the term shall survive termination for an additional two (2) years.

4. PENALTIES AND REMEDIES FOR BREACH
   In the event of unauthorized disclosure or misuse of Confidential Information,
   the breaching party shall be liable for liquidated damages of $500,000 USD per
   incident of breach, plus any actual damages proven in court, plus reasonable
   attorneys' fees and costs. The parties acknowledge that breach of this Agreement
   would cause irreparable harm for which monetary damages alone would be insufficient,
   and therefore agree that injunctive relief shall also be available as a remedy.

5. GOVERNING LAW AND JURISDICTION
   This Agreement shall be governed by and construed in accordance with the laws of
   the State of Delaware, USA, without regard to its conflict of law principles.
   Any disputes arising under this Agreement shall be resolved exclusively in the
   state or federal courts located in Delaware, and both parties consent to personal
   jurisdiction in those courts.

6. EXCLUSIONS FROM CONFIDENTIAL INFORMATION
   The obligations under this Agreement do not apply to information that the receiving
   party can demonstrate: (i) is or becomes publicly known through no act or fault of
   the receiving party; (ii) was rightfully known to the receiving party prior to
   disclosure without restriction; (iii) is independently developed by the receiving
   party without use of or reference to the disclosing party's Confidential Information;
   (iv) is disclosed with the prior written approval of the disclosing party; or
   (v) is required to be disclosed by law or court order, provided the receiving party
   gives prompt written notice to the disclosing party.

7. RETURN OR DESTRUCTION OF INFORMATION
   Upon request by the disclosing party or upon termination of this Agreement, the
   receiving party shall promptly return or destroy all tangible materials containing
   Confidential Information, including all copies, and shall certify in writing that
   it has done so.

8. NO LICENSE
   Nothing in this Agreement grants either party any license, right, title, or interest
   in any intellectual property of the other party. No party shall reverse engineer,
   decompile, or disassemble any Confidential Information disclosed by the other party.

9. ENTIRE AGREEMENT
   This Agreement constitutes the entire agreement between the parties with respect to
   the subject matter hereof and supersedes all prior agreements, understandings, and
   negotiations, whether written or oral. This Agreement may not be amended except by
   a written instrument signed by authorized representatives of both parties.
"""

# Chunk at ~600 chars with 150-char overlap — wide enough to keep full clause sections intact
from langchain_text_splitters import RecursiveCharacterTextSplitter as _RTS
_splitter = _RTS(chunk_size=600, chunk_overlap=150)
CORPUS_CHUNKS = _splitter.split_text(CORPUS)


# ── LLM-as-judge ─────────────────────────────────────────────────────────────

def llm_judge(question: str, context: str, answer: str, citations: list) -> dict:
    """
    Asks Groq to evaluate the answer and return JSON scores.
    Returns dict with keys: faithfulness, completeness, no_hallucination, reasoning, pass
    """
    from app.services.rag import get_llm
    llm = get_llm()

    prompt = f"""You are an expert RAG evaluator. Score this Q&A strictly.

DOCUMENT CONTEXT:
{context}

QUESTION: {question}

ANSWER: {answer}

CITATIONS PROVIDED: {json.dumps(citations, indent=2)}

Evaluate the answer on these 3 criteria (score 1-5, where 5 = perfect):

1. faithfulness    — Is every claim in the answer supported by the context? (1=many unsupported claims, 5=fully grounded)
2. completeness    — Does the answer address what was asked? (1=misses the point, 5=fully addresses question)
3. no_hallucination — Does the answer avoid inventing facts not in the context? (1=major hallucination, 5=no hallucination)

Also note any bugs, gaps, or issues you observe.

Respond ONLY with valid JSON, no explanation outside JSON:
{{
  "faithfulness": <1-5>,
  "completeness": <1-5>,
  "no_hallucination": <1-5>,
  "reasoning": "<one line per criterion: why you gave that score>",
  "issues": "<list any bugs or problems found, or 'none'>",
  "pass": <true if average of all 3 scores >= 4, false otherwise>
}}"""

    try:
        response = llm.invoke([HumanMessage(content=prompt)])
        raw = response.content.strip()
        # Strip markdown code fences
        if "```" in raw:
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()
        # Robustly collapse all whitespace inside JSON string values
        # (LLMs sometimes emit multi-line strings inside JSON)
        import re
        # Replace newlines + surrounding spaces inside string values with a single space
        raw = re.sub(r'(?s)("(?:[^"\\]|\\.)*")', lambda m: m.group(0).replace('\n', ' ').replace('\r', ''), raw)
        return json.loads(raw)
    except Exception as e:
        raw_content = response.content if 'response' in dir() else 'no response'
        logger.error(f"[JUDGE] Failed to parse judge response: {e}\nRaw: {raw_content}")
        return {"faithfulness": 0, "completeness": 0, "no_hallucination": 0, "reasoning": str(e), "issues": str(e), "pass": False}


# ── Build test vector store from corpus ──────────────────────────────────────

@pytest.fixture(scope="module")
def test_vectorstore():
    """Build an in-memory Chroma vector store from CORPUS_CHUNKS."""
    from langchain_community.vectorstores import Chroma
    from app.services.vector_store import get_embeddings

    embedding_fn = get_embeddings()
    docs = [
        Document(
            page_content=chunk,
            metadata={"source": "test_nda.txt", "chunk_index": i, "page_number": 1, "confidence": 0.99}
        )
        for i, chunk in enumerate(CORPUS_CHUNKS)
    ]
    vs = Chroma.from_documents(docs, embedding=embedding_fn, collection_name="test_e2e_session")
    yield vs
    vs.delete_collection()


SESSION_ID = "test_e2e_session"

# ── Test scenarios ─────────────────────────────────────────────────────────────

SCENARIOS = [
    {
        "id": "T01",
        "label": "Direct fact lookup",
        "question": "What is the penalty for breach of this agreement?",
        "expected_contains": ["500,000", "liquidated damages"],
        "advanced": False,
    },
    {
        "id": "T02",
        "label": "Date / metadata extraction",
        "question": "When was this agreement signed and between which companies?",
        "expected_contains": ["January 15, 2025", "Acme Corp", "Beta Solutions"],
        "advanced": False,
    },
    {
        "id": "T03",
        "label": "Summary request (vague question type)",
        "question": "What is in this document?",
        "expected_contains": ["confidential", "agreement"],
        "advanced": False,
    },
    {
        "id": "T04",
        "label": "Obligation / clause extraction",
        "question": "What are the obligations of each party?",
        "expected_contains": ["confidential", "disclose"],
        "advanced": False,
    },
    {
        "id": "T05",
        "label": "Term duration",
        "question": "How long does this agreement last?",
        "expected_contains": ["year"],
        "advanced": False,
    },
    {
        "id": "T06",
        "label": "Governing law",
        "question": "Which country's law governs this agreement?",
        "expected_contains": ["Delaware"],
        "advanced": False,
    },
    {
        "id": "T07",
        "label": "Exclusions / edge case",
        "question": "What information is NOT considered confidential?",
        "expected_contains": ["publicly known", "independently developed"],
        "advanced": False,
    },
    {
        "id": "T08",
        "label": "Opinion/advice (should I sign this?)",
        "question": "Should I sign this agreement? What are the risks?",
        "expected_contains": ["500,000", "confidential"],  # must use doc context
        "advanced": False,
    },
    {
        "id": "T09",
        "label": "Small talk — intent check (should NOT do retrieval)",
        "question": "Hello, how are you?",
        "expected_contains": [],  # any friendly response is fine
        "advanced": False,
        "is_small_talk": True,
    },
    {
        "id": "T10",
        "label": "Advanced mode — query rewrite + graph context",
        "question": "What obligations does Acme Corp have toward Beta Solutions?",
        "expected_contains": ["confidential", "disclose"],
        "advanced": True,
    },
]


# ── Master test runner ────────────────────────────────────────────────────────

class TestE2EPipeline:

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
    def test_scenario(self, test_vectorstore, scenario):
        from app.services.rag import answer_question, check_intent

        sid    = SESSION_ID
        q      = scenario["question"]
        adv    = scenario.get("advanced", False)
        is_st  = scenario.get("is_small_talk", False)

        logger.info(f"\n{'='*60}")
        logger.info(f"[{scenario['id']}] {scenario['label']}")
        logger.info(f"  Question : {q}")
        logger.info(f"  Advanced : {adv}")

        t0 = time.time()
        answer, elapsed_ms, citations = answer_question(
            vectorstore=test_vectorstore,
            question=q,
            session_id=sid,
            advanced=adv,
        )
        wall_ms = int((time.time() - t0) * 1000)

        logger.info(f"  Answer   : {answer[:300]}")
        logger.info(f"  Citations: {len(citations)}")
        logger.info(f"  Time     : {wall_ms}ms (reported={elapsed_ms}ms)")

        # ── Basic structural checks ──────────────────────────────────
        assert isinstance(answer, str), "answer must be a string"
        assert len(answer) > 10, f"answer too short ({len(answer)} chars)"
        assert isinstance(citations, list), "citations must be a list"
        assert elapsed_ms > 0, "elapsed_ms must be positive"

        # ── Small talk: should have 0 citations ─────────────────────
        if is_st:
            assert len(citations) == 0, (
                f"[{scenario['id']}] Small talk should produce 0 citations, got {len(citations)}"
            )
            logger.info(f"  [PASS] Small talk — no citations as expected")
            return

        # ── RAG: should have citations ───────────────────────────────
        assert len(citations) > 0, (
            f"[{scenario['id']}] RAG question produced 0 citations — vector retrieval may be broken"
        )

        # ── Keyword presence check ───────────────────────────────────
        answer_lower = answer.lower()
        for keyword in scenario.get("expected_contains", []):
            assert keyword.lower() in answer_lower, (
                f"[{scenario['id']}] Expected '{keyword}' in answer but not found.\n"
                f"Answer: {answer[:500]}"
            )

        # ── LLM-as-Judge ────────────────────────────────────────────
        context = "\n\n".join(c.get("preview", "") for c in citations)
        verdict = llm_judge(question=q, context=CORPUS, answer=answer, citations=citations)

        logger.info(f"  [JUDGE]  faithfulness={verdict.get('faithfulness')}, "
                    f"completeness={verdict.get('completeness')}, "
                    f"no_hallucination={verdict.get('no_hallucination')}")
        logger.info(f"  [JUDGE]  reasoning={verdict.get('reasoning')}")
        logger.info(f"  [JUDGE]  issues={verdict.get('issues')}")
        logger.info(f"  [JUDGE]  PASS={verdict.get('pass')}")

        # Fail test if judge flags it
        assert verdict.get("pass") is True, (
            f"\n[{scenario['id']}] LLM JUDGE FAILED\n"
            f"  faithfulness    : {verdict.get('faithfulness')}/5\n"
            f"  completeness    : {verdict.get('completeness')}/5\n"
            f"  no_hallucination: {verdict.get('no_hallucination')}/5\n"
            f"  reasoning       : {verdict.get('reasoning')}\n"
            f"  issues          : {verdict.get('issues')}\n"
            f"  answer          : {answer[:600]}\n"
        )


# ── Standalone component tests ────────────────────────────────────────────────

class TestIntentCheck:
    def test_doc_question_returns_true(self):
        from app.services.rag import check_intent
        assert check_intent("What does the document say?") is True

    def test_small_talk_returns_false(self):
        from app.services.rag import check_intent
        assert check_intent("Hello how are you?") is False

    def test_summary_request_returns_true(self):
        from app.services.rag import check_intent
        assert check_intent("Summarize the uploaded file") is True

    def test_risk_question_returns_true(self):
        from app.services.rag import check_intent
        assert check_intent("What are the risks in this agreement?") is True


class TestBM25:
    def test_bm25_returns_docs(self, test_vectorstore):
        from app.services.rag import _bm25_search
        results = _bm25_search(test_vectorstore, SESSION_ID, "penalty breach liquidated damages", top_k=3)
        assert len(results) > 0, "BM25 should return results for a keyword in corpus"

    def test_bm25_empty_query_handled(self, test_vectorstore):
        from app.services.rag import _bm25_search
        results = _bm25_search(test_vectorstore, SESSION_ID, "zzzzzzzzquerynotexist", top_k=3)
        # May return [] or low-score docs — should not raise
        assert isinstance(results, list)

    def test_bm25_cache_hit(self, test_vectorstore):
        from app.services.rag import _bm25_search, _bm25_cache
        _bm25_search(test_vectorstore, SESSION_ID, "test", top_k=2)
        assert SESSION_ID in _bm25_cache, "BM25 index should be cached after first call"


class TestHybridRetrieve:
    def test_returns_docs(self, test_vectorstore):
        from app.services.rag import _hybrid_retrieve
        docs = _hybrid_retrieve(test_vectorstore, SESSION_ID, ["penalty breach"])
        assert len(docs) > 0

    def test_returns_at_most_rerank_top_n(self, test_vectorstore):
        from app.services.rag import _hybrid_retrieve
        from app.config import cfg
        docs = _hybrid_retrieve(test_vectorstore, SESSION_ID, ["agreement obligations term"])
        assert len(docs) <= cfg.rerank_top_n + 2  # slight buffer for reranker fallback


class TestNeo4j:
    def test_driver_connects(self):
        from app.services.graph_store import _get_driver
        driver = _get_driver()
        assert driver is not None, "Neo4j driver should connect with .env credentials"

    def test_build_and_query_graph(self):
        from app.services.graph_store import build_graph, query_graph
        from app.services.rag import get_llm
        llm = get_llm()
        test_sid = "neo4j_unit_test_session_001"

        ok = build_graph(CORPUS_CHUNKS[:3], test_sid, llm)
        assert ok is True, "build_graph should return True on success"

        result = query_graph("Acme Corp obligations", test_sid)
        assert isinstance(result, str), "query_graph must return a string"
        logger.info(f"[NEO4J TEST] graph result: {result[:300]}")

    def test_query_graph_empty_session_returns_empty(self):
        from app.services.graph_store import query_graph
        result = query_graph("some question", "nonexistent_session_xyz_000")
        assert result == "", "Empty session should return empty string"


class TestLatency:
    """All latency checks — flags slow paths as bugs."""

    def test_intent_check_under_5s(self):
        from app.services.rag import check_intent
        t0 = time.time()
        check_intent("What does the document say?")
        elapsed = time.time() - t0
        assert elapsed < 5.0, f"Intent check too slow: {elapsed:.1f}s (budget: 5s)"

    def test_simple_pipeline_under_30s(self, test_vectorstore):
        from app.services.rag import answer_question
        t0 = time.time()
        answer_question(test_vectorstore, "What is the term of this agreement?", SESSION_ID, advanced=False)
        elapsed = time.time() - t0
        assert elapsed < 30.0, f"Simple pipeline too slow: {elapsed:.1f}s (budget: 30s)"

    def test_advanced_pipeline_under_60s(self, test_vectorstore):
        from app.services.rag import answer_question
        t0 = time.time()
        answer_question(test_vectorstore, "What are the obligations?", SESSION_ID, advanced=True)
        elapsed = time.time() - t0
        assert elapsed < 60.0, f"Advanced pipeline too slow: {elapsed:.1f}s (budget: 60s)"


class TestFallback:
    """Advanced mode must fall back gracefully when something breaks."""

    def test_advanced_fallback_on_broken_vectorstore(self):
        from unittest.mock import MagicMock
        from app.services.rag import answer_question
        from langchain_core.documents import Document

        broken_vs = MagicMock()
        broken_vs.as_retriever.side_effect = RuntimeError("simulated MMR failure")
        broken_vs._collection = MagicMock()
        broken_vs._collection.get.return_value = {"documents": [], "metadatas": []}

        answer, elapsed_ms, citations = answer_question(
            vectorstore=broken_vs,
            question="What is in this document?",
            session_id="fallback_test_session",
            advanced=True,
        )
        assert isinstance(answer, str), "Must return a string even on full failure"
        assert len(answer) > 0, "Must return non-empty answer even on failure"
        logger.info(f"[FALLBACK TEST] answer: {answer[:200]}")

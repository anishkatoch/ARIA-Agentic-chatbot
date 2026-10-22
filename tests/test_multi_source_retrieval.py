"""
Tests for multi-source retrieval in app/services/rag.py - answering a
single chat turn against the personal session AND any number of attached
knowledge bases together (see app.routers.chat._resolve_extra_sources and
answer_question's extra_sources parameter).

No LLM/vectorstore network calls: _retrieve_for_question (the thing that
would normally hit a real vectorstore + BM25 index) is patched per source,
same "no real retrieval" pattern as tests/test_clarification.py's ReAct
tests use for create_agent.
"""
import pytest
from unittest.mock import MagicMock, patch
from langchain_core.documents import Document

from app.services.rag import (
    _merge_source_docs,
    _label_chunk,
    _build_citations,
    _cache_key,
)


def _doc(text, source="doc.pdf"):
    return Document(page_content=text, metadata={"source": source})


class TestMergeSourceDocsSingleSource:
    def test_single_source_skips_merge_and_rerank(self):
        """The common case (nothing attached) must cost exactly what it did
        before multi-source support existed - no extra RRF/rerank pass."""
        docs = [_doc("a"), _doc("b")]
        sources = [{"vectorstore": MagicMock(), "session_id": "s1", "label": None}]
        with patch("app.services.rag._retrieve_for_question", return_value=docs) as mock_retrieve, \
             patch("app.services.rag._rrf_merge") as mock_rrf, \
             patch("app.services.rag._rerank") as mock_rerank:
            result = _merge_source_docs(sources, "question", ["question"])

        mock_retrieve.assert_called_once_with(sources[0]["vectorstore"], "s1", "question", ["question"])
        mock_rrf.assert_not_called()
        mock_rerank.assert_not_called()
        assert result == docs

    def test_single_source_tags_origin_label_none(self):
        docs = [_doc("a")]
        sources = [{"vectorstore": MagicMock(), "session_id": "s1", "label": None}]
        with patch("app.services.rag._retrieve_for_question", return_value=docs):
            result = _merge_source_docs(sources, "question", ["question"])
        assert result[0].metadata["origin_label"] is None


class TestMergeSourceDocsMultiSource:
    def test_multi_source_merges_and_reranks(self):
        personal_docs = [_doc("from personal", "mine.pdf")]
        kb_docs = [_doc("from kb", "policy.pdf")]
        sources = [
            {"vectorstore": MagicMock(), "session_id": "s1", "label": None},
            {"vectorstore": MagicMock(), "session_id": "kb-session", "label": "HR Policies"},
        ]

        def fake_retrieve(vectorstore, session_id, question, queries):
            return personal_docs if session_id == "s1" else kb_docs

        with patch("app.services.rag._retrieve_for_question", side_effect=fake_retrieve), \
             patch("app.services.rag._rrf_merge", return_value=personal_docs + kb_docs) as mock_rrf, \
             patch("app.services.rag._rerank", side_effect=lambda q, docs, n: docs) as mock_rerank:
            result = _merge_source_docs(sources, "question", ["question"])

        mock_rrf.assert_called_once()
        mock_rerank.assert_called_once()
        assert personal_docs[0].metadata["origin_label"] is None
        assert kb_docs[0].metadata["origin_label"] == "HR Policies"
        assert result == personal_docs + kb_docs

    def test_one_source_failing_does_not_block_the_others(self):
        """_retrieve_for_question already swallows its own per-source
        errors (broad-context/hybrid fallback) - this just confirms
        _merge_source_docs doesn't add a new failure mode across sources
        when one of them legitimately returns nothing."""
        sources = [
            {"vectorstore": MagicMock(), "session_id": "s1", "label": None},
            {"vectorstore": MagicMock(), "session_id": "kb-session", "label": "Empty KB"},
        ]

        def fake_retrieve(vectorstore, session_id, question, queries):
            return [] if session_id == "kb-session" else [_doc("ok")]

        with patch("app.services.rag._retrieve_for_question", side_effect=fake_retrieve), \
             patch("app.services.rag._rrf_merge", side_effect=lambda lists, top_k: [d for l in lists for d in l]), \
             patch("app.services.rag._rerank", side_effect=lambda q, docs, n: docs):
            result = _merge_source_docs(sources, "question", ["question"])
        assert len(result) == 1
        assert result[0].page_content == "ok"


class TestLabelChunk:
    def test_no_prefix_when_origin_label_unset(self):
        doc = _doc("hello")
        assert _label_chunk(doc) == "hello"

    def test_prefixed_with_source_label_when_set(self):
        doc = _doc("hello")
        doc.metadata["origin_label"] = "HR Policies"
        assert _label_chunk(doc) == "[Source: HR Policies]\nhello"


class TestBuildCitationsOriginLabel:
    def test_knowledge_base_field_present_and_none_by_default(self):
        citations = _build_citations([_doc("x")])
        assert citations[0]["knowledge_base"] is None

    def test_knowledge_base_field_reflects_origin_label(self):
        doc = _doc("x")
        doc.metadata["origin_label"] = "Finance Reports"
        citations = _build_citations([doc])
        assert citations[0]["knowledge_base"] == "Finance Reports"


class TestCacheKeyIncludesExtraSources:
    def test_different_attached_kb_sets_produce_different_keys(self):
        k1 = _cache_key("s1", "same question", advanced=False, extra_session_ids=())
        k2 = _cache_key("s1", "same question", advanced=False, extra_session_ids=("kb-session-a",))
        k3 = _cache_key("s1", "same question", advanced=False, extra_session_ids=("kb-session-b",))
        assert k1 != k2 != k3 != k1

    def test_same_extra_sources_regardless_of_order_produce_same_key(self):
        """Attachment order shouldn't matter for caching - two sessions with
        the same two KBs attached in a different order are the same query."""
        k1 = _cache_key("s1", "q", advanced=False, extra_session_ids=("a", "b"))
        k2 = _cache_key("s1", "q", advanced=False, extra_session_ids=("b", "a"))
        assert k1 == k2

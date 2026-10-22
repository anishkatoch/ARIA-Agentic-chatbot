"""
Tests for the source-resolution / broad-query feature in app/services/rag.py.

Covers the class of question pure semantic similarity can't answer: "what is
in this document", "what's in the excel", "what is in this chat/inbox/mail",
and synthesis-style asks ("key points", "risks") that aren't phrased verbatim
in the source text.

No LLM calls. Vector-store-touching helpers (list_session_sources,
_fetch_broad_context) use a MagicMock store, same pattern as
tests/test_clarification.py.
"""
import pytest
from unittest.mock import MagicMock
from langchain_core.documents import Document

from app.services.rag import (
    is_broad_query,
    _resolve_target_sources,
    list_session_sources,
    _fetch_broad_context,
    _fetch_all_docs_and_metas,
)


# ── is_broad_query ────────────────────────────────────────────────────────────

class TestIsBroadQuery:
    @pytest.mark.parametrize("q", [
        "what is in this document",
        "what is in the data",
        "what this file contains",
        "what does this file contain",
        "what's in this excel",
        "summarize the report",
        "give me an overview",
        "what are the key points",
        "what are the important points",
        "what are the risk points",
        "any risks in this contract",
        "tell me about this document",
        "describe the document",
        "what are the takeaways",
    ])
    def test_detects_broad_intent(self, q):
        assert is_broad_query(q) is True

    @pytest.mark.parametrize("q", [
        "hi",
        "thanks",
        "what is the termination notice period",
        "who signed the contract",
        "is there a leave policy section",
        "what is the total in row 5",
    ])
    def test_does_not_flag_specific_questions(self, q):
        assert is_broad_query(q) is False


# ── _resolve_target_sources ───────────────────────────────────────────────────

class TestResolveTargetSources:
    def setup_method(self):
        self.sources = [
            {"source": "Food-Safety-Policy-01.04.19.pdf", "connector": None},
            {"source": "budget_report.xlsx", "connector": None},
            {"source": "Google Chat - team-standup.txt", "connector": "google_chat"},
            {"source": "Project kickoff (abcd1234).txt", "connector": "gmail"},
        ]

    def test_no_sources_returns_none(self):
        assert _resolve_target_sources("what is in this", []) is None

    def test_filename_substring_match(self):
        result = _resolve_target_sources("what is in the food safety policy", self.sources)
        assert result == ["Food-Safety-Policy-01.04.19.pdf"]

    def test_filename_match_ignores_separator_punctuation(self):
        """Filenames use underscores ('budget_report.xlsx'); users type spaces."""
        result = _resolve_target_sources("summarize the budget report", self.sources)
        assert result == ["budget_report.xlsx"]

    def test_extension_alias_excel(self):
        result = _resolve_target_sources("what is in this excel", self.sources)
        assert result == ["budget_report.xlsx"]

    def test_connector_alias_chat(self):
        result = _resolve_target_sources("what is in this chat", self.sources)
        assert result == ["Google Chat - team-standup.txt"]

    def test_connector_alias_space(self):
        result = _resolve_target_sources("what is in this space", self.sources)
        assert result == ["Google Chat - team-standup.txt"]

    def test_connector_alias_inbox_and_mail(self):
        for q in ["what is in my inbox", "what is in this mail", "what's in this email"]:
            assert _resolve_target_sources(q, self.sources) == ["Project kickoff (abcd1234).txt"]

    def test_demonstrative_with_multiple_sources_is_ambiguous(self):
        # "this document" with 4 distinct sources in the session can't be
        # safely resolved to one - must return None (normal/ambiguity-aware
        # retrieval should take over), not silently pick one.
        assert _resolve_target_sources("what is in this document", self.sources) is None

    def test_demonstrative_with_single_source_resolves(self):
        single = [{"source": "xyz_report.pdf", "connector": None}]
        assert _resolve_target_sources("what is in this document", single) == ["xyz_report.pdf"]
        assert _resolve_target_sources("what is in this file", single) == ["xyz_report.pdf"]

    def test_distinctive_token_fallback_single_source(self):
        """'xyz' in the question matches 'xyz_report.pdf' via its stem token,
        even though the full stem ('xyz report') isn't a substring of the
        question - this is the exact "filename xyz" case from the bug report."""
        single = [{"source": "xyz_report.pdf", "connector": None}]
        assert _resolve_target_sources("what is in this xyz", single) == ["xyz_report.pdf"]

    def test_generic_stem_words_dont_false_positive(self):
        multi = [
            {"source": "xyz_report.pdf", "connector": None},
            {"source": "abc_summary.docx", "connector": None},
        ]
        # "report" alone is a generic word shared by many filenames - must
        # not resolve to a specific source just because it appears in both.
        assert _resolve_target_sources("what is in this report", multi) is None

    def test_unrelated_question_returns_none(self):
        assert _resolve_target_sources("what is the weather today", self.sources) is None

    def test_multiple_named_files_scopes_to_all(self):
        result = _resolve_target_sources(
            "compare the food safety policy and the budget report", self.sources
        )
        assert set(result) == {"Food-Safety-Policy-01.04.19.pdf", "budget_report.xlsx"}


# ── list_session_sources / _fetch_broad_context (vector-store-touching) ──────

class TestFetchAllDocsAndMetas:
    def test_chroma_backend(self):
        vs = MagicMock()
        vs._collection = MagicMock()
        vs._collection.get.return_value = {
            "documents": ["a", "b"],
            "metadatas": [{"source": "x.pdf"}, {"source": "y.pdf"}],
        }
        docs, metas = _fetch_all_docs_and_metas(vs)
        assert docs == ["a", "b"]
        assert metas == [{"source": "x.pdf"}, {"source": "y.pdf"}]

    def test_pgvector_backend_no_collection_name_returns_empty(self):
        vs = MagicMock(spec=[])  # no _collection, no collection_name attr
        docs, metas = _fetch_all_docs_and_metas(vs)
        assert docs == []
        assert metas == []


class TestListSessionSources:
    def _make_vs(self, rows: list[dict]):
        vs = MagicMock()
        vs._collection = MagicMock()
        vs._collection.get.return_value = {
            "documents": [f"chunk {i}" for i in range(len(rows))],
            "metadatas": rows,
        }
        return vs

    def test_deduplicates_sources(self):
        vs = self._make_vs([
            {"source": "a.pdf", "chunk_index": 0},
            {"source": "a.pdf", "chunk_index": 1},
            {"source": "b.pdf", "chunk_index": 0},
        ])
        result = list_session_sources(vs)
        assert sorted(s["source"] for s in result) == ["a.pdf", "b.pdf"]

    def test_carries_connector_metadata(self):
        vs = self._make_vs([
            {"source": "Google Chat - x.txt", "connector": "google_chat"},
        ])
        result = list_session_sources(vs)
        assert result[0]["connector"] == "google_chat"

    def test_empty_store_returns_empty_list(self):
        vs = self._make_vs([])
        assert list_session_sources(vs) == []

    def test_chunks_with_no_source_are_skipped(self):
        vs = self._make_vs([{"chunk_index": 0}])
        assert list_session_sources(vs) == []


class TestFetchBroadContext:
    def _make_vs(self, rows: list[tuple[str, dict]]):
        vs = MagicMock()
        vs._collection = MagicMock()
        vs._collection.get.return_value = {
            "documents": [r[0] for r in rows],
            "metadatas": [r[1] for r in rows],
        }
        return vs

    def test_scopes_to_target_source_in_order(self):
        rows = [
            ("chunk 2", {"source": "a.pdf", "chunk_index": 2}),
            ("chunk 0", {"source": "a.pdf", "chunk_index": 0}),
            ("other",   {"source": "b.pdf", "chunk_index": 0}),
            ("chunk 1", {"source": "a.pdf", "chunk_index": 1}),
        ]
        vs = self._make_vs(rows)
        docs = _fetch_broad_context(vs, "sess", ["a.pdf"], limit=10)
        assert [d.page_content for d in docs] == ["chunk 0", "chunk 1", "chunk 2"]

    def test_respects_limit(self):
        rows = [(f"c{i}", {"source": "a.pdf", "chunk_index": i}) for i in range(10)]
        vs = self._make_vs(rows)
        docs = _fetch_broad_context(vs, "sess", ["a.pdf"], limit=3)
        assert len(docs) == 3

    def test_no_target_spreads_across_sources(self):
        rows = (
            [(f"a{i}", {"source": "a.pdf", "chunk_index": i}) for i in range(5)] +
            [(f"b{i}", {"source": "b.pdf", "chunk_index": i}) for i in range(5)]
        )
        vs = self._make_vs(rows)
        docs = _fetch_broad_context(vs, "sess", None, limit=4)
        sources_seen = {d.metadata["source"] for d in docs}
        # Must not be starved to just one source when nothing was named.
        assert sources_seen == {"a.pdf", "b.pdf"}

    def test_empty_store_returns_empty_list(self):
        vs = self._make_vs([])
        assert _fetch_broad_context(vs, "sess", None) == []

    def test_unknown_target_source_returns_empty(self):
        rows = [("c0", {"source": "a.pdf", "chunk_index": 0})]
        vs = self._make_vs(rows)
        assert _fetch_broad_context(vs, "sess", ["nonexistent.pdf"]) == []

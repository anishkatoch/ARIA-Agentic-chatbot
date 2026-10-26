"""Tests for app/services/compare_query.py - chat-level "what changed" /
"difference between X and Y" queries. MagicMock DB session, same pattern as
tests/test_session_attachments.py - no real DB round trip, no real LLM call."""
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.services import compare_query


def _doc(id_, kb_id, filename, version, item_id=None, extracted_text="text", uploaded_at=None):
    return MagicMock(
        id=id_, knowledge_base_id=kb_id, filename=filename, version=version,
        item_id=item_id, extracted_text=extracted_text,
        uploaded_at=uploaded_at or datetime(2026, 1, version, tzinfo=timezone.utc),
    )


class TestDetectCompareIntent:
    def test_detects_difference_phrasing(self):
        assert compare_query.detect_compare_intent("what is the difference between abc and xyz")

    def test_detects_what_changed(self):
        assert compare_query.detect_compare_intent("what changed in food_rights.pdf between v1 and v2")

    def test_detects_updated_version(self):
        assert compare_query.detect_compare_intent("show me the updated version of the pricing sheet")

    def test_plain_question_is_not_compare_intent(self):
        assert not compare_query.detect_compare_intent("what is the eligibility age in food_rights.pdf?")


class TestPickVersion:
    def test_picks_by_explicit_version_number(self):
        chain = {"versions": [_doc(uuid.uuid4(), 1, "f.pdf", 1), _doc(uuid.uuid4(), 1, "f.pdf", 2)]}
        row = compare_query._pick_version(chain, 2, None)
        assert row.version == 2

    def test_picks_latest(self):
        chain = {"versions": [_doc(uuid.uuid4(), 1, "f.pdf", 1), _doc(uuid.uuid4(), 1, "f.pdf", 2)]}
        row = compare_query._pick_version(chain, None, "latest")
        assert row.version == 2

    def test_picks_previous(self):
        chain = {"versions": [_doc(uuid.uuid4(), 1, "f.pdf", 1), _doc(uuid.uuid4(), 1, "f.pdf", 2)]}
        row = compare_query._pick_version(chain, None, "previous")
        assert row.version == 1

    def test_missing_version_number_returns_none(self):
        chain = {"versions": [_doc(uuid.uuid4(), 1, "f.pdf", 1)]}
        assert compare_query._pick_version(chain, 5, None) is None


class TestMentionedChains:
    def test_matches_filename_stem(self):
        chains = [{"names": {"food rights"}, "versions": []}, {"names": {"budget report"}, "versions": []}]
        hits = compare_query._mentioned_chains("what changed in food_rights.pdf between v1 and v2", chains)
        assert len(hits) == 1

    def test_matches_two_distinct_documents(self):
        chains = [{"names": {"abc"}, "versions": []}, {"names": {"xyz"}, "versions": []}]
        hits = compare_query._mentioned_chains("what's the difference between abc.pdf and xyz.pdf", chains)
        assert len(hits) == 2


class TestResolveCompareTargets:
    def _patched_factory(self, chains_return):
        db_mock = MagicMock()
        factory = MagicMock(return_value=db_mock)
        return db_mock, factory

    def test_no_kb_sources_returns_none(self):
        # Personal-session-only sources (label=None) - feature only works
        # for KB-attached documents.
        sources = [{"vectorstore": object(), "session_id": "s1", "label": None}]
        result = compare_query.resolve_compare_targets("what changed between v1 and v2", sources)
        assert result is None

    def test_single_doc_version_pair_resolves(self):
        kb_id = uuid.uuid4()
        v1 = _doc(uuid.uuid4(), kb_id, "food_rights.pdf", 1, extracted_text="age is 18")
        v2 = _doc(uuid.uuid4(), kb_id, "food_rights.pdf", 2, extracted_text="age is 16")

        db_mock = MagicMock()
        kb_row = MagicMock(id=kb_id)
        db_mock.query.return_value.filter.return_value.all.side_effect = [
            [kb_row],       # KnowledgeBase lookup by session_id
            [v1, v2],       # KnowledgeBaseDocument rows for this kb
        ]
        factory = MagicMock(return_value=db_mock)

        sources = [{"vectorstore": object(), "session_id": "kb-session-1", "label": "HR Docs"}]
        with patch("app.db.session.get_session_factory", return_value=factory):
            result = compare_query.resolve_compare_targets(
                "what changed in food_rights.pdf between version 1 and version 2?", sources
            )
        assert result is not None
        doc_a, doc_b, label_a, label_b = result
        assert doc_a.version == 1
        assert doc_b.version == 2

    def test_two_distinct_documents_resolve_to_latest_each(self):
        kb_id = uuid.uuid4()
        abc = _doc(uuid.uuid4(), kb_id, "abc.pdf", 1, extracted_text="abc content")
        xyz = _doc(uuid.uuid4(), kb_id, "xyz.pdf", 1, extracted_text="xyz content")

        db_mock = MagicMock()
        kb_row = MagicMock(id=kb_id)
        db_mock.query.return_value.filter.return_value.all.side_effect = [
            [kb_row],
            [abc, xyz],
        ]
        factory = MagicMock(return_value=db_mock)

        sources = [{"vectorstore": object(), "session_id": "kb-session-1", "label": "HR Docs"}]
        with patch("app.db.session.get_session_factory", return_value=factory):
            result = compare_query.resolve_compare_targets(
                "what's the difference between abc.pdf and xyz.pdf?", sources
            )
        assert result is not None
        doc_a, doc_b, _, _ = result
        assert {doc_a.filename, doc_b.filename} == {"abc.pdf", "xyz.pdf"}

    def test_vague_question_falls_back_to_single_document_with_history(self):
        """'what changed in this document?' / 'what changed between v1 and
        v2?' with no filename named at all - should still resolve when
        exactly one document in scope has version history to compare."""
        kb_id = uuid.uuid4()
        v1 = _doc(uuid.uuid4(), kb_id, "document_v1.pdf", 1, item_id="1101", extracted_text="old text")
        v2 = _doc(uuid.uuid4(), kb_id, "document_v2_updated.pdf", 2, item_id="1101", extracted_text="new text")

        db_mock = MagicMock()
        kb_row = MagicMock(id=kb_id)
        db_mock.query.return_value.filter.return_value.all.side_effect = [[kb_row], [v1, v2]]
        factory = MagicMock(return_value=db_mock)

        sources = [{"vectorstore": object(), "session_id": "kb-session-1", "label": "Document versioning"}]
        with patch("app.db.session.get_session_factory", return_value=factory):
            result = compare_query.resolve_compare_targets(
                "what is the change were there in v1 and v2 in this document", sources
            )
        assert result is not None
        doc_a, doc_b, _, _ = result
        assert doc_a.version == 1
        assert doc_b.version == 2

    def test_vague_question_with_multiple_candidates_stays_unresolved(self):
        """Two different documents both have version history and neither is
        named - genuinely ambiguous, must not guess."""
        kb_id = uuid.uuid4()
        a1 = _doc(uuid.uuid4(), kb_id, "alpha.pdf", 1, item_id="a")
        a2 = _doc(uuid.uuid4(), kb_id, "alpha.pdf", 2, item_id="a")
        b1 = _doc(uuid.uuid4(), kb_id, "beta.pdf", 1, item_id="b")
        b2 = _doc(uuid.uuid4(), kb_id, "beta.pdf", 2, item_id="b")

        db_mock = MagicMock()
        kb_row = MagicMock(id=kb_id)
        db_mock.query.return_value.filter.return_value.all.side_effect = [[kb_row], [a1, a2, b1, b2]]
        factory = MagicMock(return_value=db_mock)

        sources = [{"vectorstore": object(), "session_id": "kb-session-1", "label": "KB"}]
        with patch("app.db.session.get_session_factory", return_value=factory):
            result = compare_query.resolve_compare_targets("what changed between v1 and v2?", sources)
        assert result is None

    def test_single_version_document_cannot_be_compared(self):
        kb_id = uuid.uuid4()
        v1 = _doc(uuid.uuid4(), kb_id, "solo.pdf", 1)

        db_mock = MagicMock()
        kb_row = MagicMock(id=kb_id)
        db_mock.query.return_value.filter.return_value.all.side_effect = [[kb_row], [v1]]
        factory = MagicMock(return_value=db_mock)

        sources = [{"vectorstore": object(), "session_id": "kb-session-1", "label": "HR Docs"}]
        with patch("app.db.session.get_session_factory", return_value=factory):
            result = compare_query.resolve_compare_targets(
                "what changed in solo.pdf?", sources
            )
        assert result is None


class TestBuildCompareAnswer:
    def test_identical_text_reports_no_differences(self):
        kb_id = uuid.uuid4()
        doc_a = _doc(uuid.uuid4(), kb_id, "f.pdf", 1, extracted_text="same text")
        doc_b = _doc(uuid.uuid4(), kb_id, "f.pdf", 2, extracted_text="same text")
        answer, citations = compare_query.build_compare_answer(
            "what changed?", doc_a, doc_b, "f.pdf (v1)", "f.pdf (v2)"
        )
        assert "no differences" in answer.lower()
        assert len(citations) == 2

    def test_missing_extracted_text_gives_processing_message(self):
        kb_id = uuid.uuid4()
        doc_a = _doc(uuid.uuid4(), kb_id, "f.pdf", 1, extracted_text=None)
        doc_b = _doc(uuid.uuid4(), kb_id, "f.pdf", 2, extracted_text="new text")
        answer, _ = compare_query.build_compare_answer(
            "what changed?", doc_a, doc_b, "f.pdf (v1)", "f.pdf (v2)"
        )
        assert "hasn't finished processing" in answer or "processing" in answer

    def test_changed_text_calls_llm_and_includes_summary(self):
        kb_id = uuid.uuid4()
        doc_a = _doc(uuid.uuid4(), kb_id, "f.pdf", 1, extracted_text="Eligibility age is 18")
        doc_b = _doc(uuid.uuid4(), kb_id, "f.pdf", 2, extracted_text="Eligibility age is 16")

        fake_response = MagicMock(content="Previously: age 18 / Now: age 16")
        with patch("app.services.rag.llm_invoke", return_value=fake_response):
            answer, citations = compare_query.build_compare_answer(
                "what changed?", doc_a, doc_b, "f.pdf (v1)", "f.pdf (v2)"
            )
        assert "Previously: age 18 / Now: age 16" in answer
        assert len(citations) == 2

    def test_llm_failure_falls_back_to_line_counts(self):
        kb_id = uuid.uuid4()
        doc_a = _doc(uuid.uuid4(), kb_id, "f.pdf", 1, extracted_text="old content here")
        doc_b = _doc(uuid.uuid4(), kb_id, "f.pdf", 2, extracted_text="new content here")

        with patch("app.services.rag.llm_invoke", side_effect=RuntimeError("provider down")):
            answer, _ = compare_query.build_compare_answer(
                "what changed?", doc_a, doc_b, "f.pdf (v1)", "f.pdf (v2)"
            )
        assert "added" in answer and "removed" in answer


class TestTryCompareAnswer:
    def test_non_compare_question_returns_none(self):
        sources = [{"vectorstore": object(), "session_id": "s1", "label": "KB"}]
        assert compare_query.try_compare_answer("what is the eligibility age?", sources) is None

    def test_never_raises_on_internal_error(self):
        sources = [{"vectorstore": object(), "session_id": "s1", "label": "KB"}]
        with patch("app.services.compare_query.resolve_compare_targets", side_effect=RuntimeError("boom")):
            result = compare_query.try_compare_answer("what changed between v1 and v2?", sources)
        assert result is None

    def test_full_pipeline_end_to_end(self):
        kb_id = uuid.uuid4()
        v1 = _doc(uuid.uuid4(), kb_id, "food_rights.pdf", 1, extracted_text="Eligibility age is 18")
        v2 = _doc(uuid.uuid4(), kb_id, "food_rights.pdf", 2, extracted_text="Eligibility age is 16")

        db_mock = MagicMock()
        kb_row = MagicMock(id=kb_id)
        db_mock.query.return_value.filter.return_value.all.side_effect = [[kb_row], [v1, v2]]
        factory = MagicMock(return_value=db_mock)

        sources = [{"vectorstore": object(), "session_id": "kb-session-1", "label": "HR Docs"}]
        fake_response = MagicMock(content="Previously: age 18 / Now: age 16")
        with patch("app.db.session.get_session_factory", return_value=factory), \
             patch("app.services.rag.llm_invoke", return_value=fake_response):
            result = compare_query.try_compare_answer(
                "what changed in food_rights.pdf between version 1 and version 2?", sources
            )
        assert result is not None
        answer, citations = result
        assert "Previously: age 18 / Now: age 16" in answer
        assert len(citations) == 2

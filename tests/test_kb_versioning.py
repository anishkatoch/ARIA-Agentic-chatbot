"""Tests for app/services/kb_versioning.py - duplicate/version detection for
Knowledge Base uploads. MagicMock session, same pattern as
tests/test_session_attachments.py - no real DB round trip."""
import uuid
from unittest.mock import MagicMock

from app.services import kb_versioning


def _mock_query_returns(db_mock, row):
    # find_active_document() always chains exactly two .filter() calls
    # (base filter, then an item_id- or filename-scoped filter) before
    # .first() - see app/services/kb_versioning.py.
    db_mock.query.return_value.filter.return_value.filter.return_value.first.return_value = row


class TestDecideUploadAction:
    def test_new_document_when_nothing_exists(self):
        db_mock = MagicMock()
        _mock_query_returns(db_mock, None)
        kb_id = uuid.uuid4()
        decision = kb_versioning.decide_upload_action(db_mock, kb_id, "report.pdf", None, "hash1")
        assert decision.action == kb_versioning.NEW
        assert decision.existing is None

    def test_exact_duplicate_same_hash(self):
        db_mock = MagicMock()
        existing = MagicMock(content_hash="hash1")
        _mock_query_returns(db_mock, existing)
        kb_id = uuid.uuid4()
        decision = kb_versioning.decide_upload_action(db_mock, kb_id, "report.pdf", None, "hash1")
        assert decision.action == kb_versioning.DUPLICATE
        assert decision.existing is existing

    def test_version_bump_when_hash_differs(self):
        db_mock = MagicMock()
        existing = MagicMock(content_hash="hash1", version=1)
        _mock_query_returns(db_mock, existing)
        kb_id = uuid.uuid4()
        decision = kb_versioning.decide_upload_action(db_mock, kb_id, "report.pdf", None, "hash2")
        assert decision.action == kb_versioning.VERSION
        assert decision.existing is existing

    def test_item_id_lookup_ignores_filename(self):
        """When item_id is supplied, the lookup must be scoped by item_id,
        not filename - this is what lets a renamed file still resolve to
        its prior version."""
        db_mock = MagicMock()
        existing = MagicMock(content_hash="hash1", version=1)
        _mock_query_returns(db_mock, existing)
        kb_id = uuid.uuid4()

        decision = kb_versioning.decide_upload_action(
            db_mock, kb_id, "renamed_report_v2.pdf", "pricing-sheet", "hash2"
        )
        assert decision.action == kb_versioning.VERSION
        assert decision.existing is existing
        # Exactly one base .filter() then one item_id-scoped .filter() -
        # filename never enters the lookup when item_id is supplied.
        db_mock.query.return_value.filter.assert_called_once()
        db_mock.query.return_value.filter.return_value.filter.assert_called_once()


class TestStartNewVersion:
    def test_new_version_has_no_previous(self):
        db_mock = MagicMock()
        kb_id = uuid.uuid4()
        doc = kb_versioning.start_new_version(
            db_mock, kb_id, "report.pdf", 1024, None, "hash1", previous=None
        )
        assert doc.version == 1
        assert doc.previous_version_id is None
        assert doc.is_superseded is False
        db_mock.add.assert_called_once()
        db_mock.flush.assert_called_once()

    def test_version_bump_supersedes_previous_and_links_it(self):
        db_mock = MagicMock()
        kb_id = uuid.uuid4()
        previous = MagicMock(version=1, id=uuid.uuid4(), is_superseded=False)

        doc = kb_versioning.start_new_version(
            db_mock, kb_id, "report.pdf", 2048, None, "hash2", previous=previous
        )

        assert previous.is_superseded is True
        assert doc.version == 2
        assert doc.previous_version_id == previous.id
        assert doc.is_superseded is False

    def test_item_id_and_filename_both_stored_on_new_row(self):
        db_mock = MagicMock()
        kb_id = uuid.uuid4()
        doc = kb_versioning.start_new_version(
            db_mock, kb_id, "Pricing_Updated_Nov2026.pdf", 10, "pricing-sheet", "hash2",
            previous=MagicMock(version=1, id=uuid.uuid4(), is_superseded=False),
        )
        assert doc.item_id == "pricing-sheet"
        assert doc.filename == "Pricing_Updated_Nov2026.pdf"

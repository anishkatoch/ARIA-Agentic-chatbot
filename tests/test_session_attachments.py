"""
Tests for app/services/session_attachments.py - the server-side record of
which knowledge bases are attached to a chat session. This is the actual
fix for the recurring bug where attaching a knowledge base, then uploading
a document directly in chat, silently detached it: attachment used to live
only in frontend state (cleared on every new upload); it now lives in the
session_knowledge_bases table, which uploads never touch.

DB-touching functions use a MagicMock session, same pattern as
tests/test_kb_cache.py - no real DB round trip.
"""
import uuid
import pytest
from unittest.mock import MagicMock, patch

from app.services import session_attachments


def _factory_with(db_mock):
    return MagicMock(return_value=db_mock)


class TestAttachKb:
    def test_inserts_row_when_not_already_attached(self):
        session_id, kb_id = uuid.uuid4(), uuid.uuid4()
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.return_value = None
        with patch("app.services.session_attachments.get_session_factory",
                   return_value=_factory_with(db_mock)):
            session_attachments.attach_kb(session_id, kb_id)
        db_mock.add.assert_called()
        db_mock.commit.assert_called()

    def test_idempotent_when_already_attached(self):
        """Attaching the same KB twice must not insert a second row (and
        must not raise on the composite primary key)."""
        session_id, kb_id = uuid.uuid4(), uuid.uuid4()
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.return_value = MagicMock()  # already exists
        with patch("app.services.session_attachments.get_session_factory",
                   return_value=_factory_with(db_mock)):
            session_attachments.attach_kb(session_id, kb_id)
        # Only the (harmless, rolled-back-on-conflict) session-row insert
        # attempt happens - no SessionKnowledgeBase row added since one
        # already exists.
        from app.models.db import SessionKnowledgeBase
        added_skb_rows = [c for c in db_mock.add.call_args_list if isinstance(c.args[0], SessionKnowledgeBase)]
        assert added_skb_rows == []

    def test_ensures_session_row_exists_before_attaching(self):
        """documents.session_id and session_knowledge_bases.session_id both
        have a FK -> sessions.id - attaching a KB before any upload must
        still work, by creating the session row first."""
        session_id, kb_id = uuid.uuid4(), uuid.uuid4()
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.return_value = None
        with patch("app.services.session_attachments.get_session_factory",
                   return_value=_factory_with(db_mock)):
            session_attachments.attach_kb(session_id, kb_id)
        from app.models.db import Session
        session_rows = [c for c in db_mock.add.call_args_list if isinstance(c.args[0], Session)]
        assert len(session_rows) == 1
        assert session_rows[0].args[0].id == session_id

    def test_raises_when_db_unavailable(self):
        with patch("app.services.session_attachments.get_session_factory", return_value=None):
            with pytest.raises(RuntimeError):
                session_attachments.attach_kb(uuid.uuid4(), uuid.uuid4())


class TestDetachKb:
    def test_deletes_matching_row(self):
        session_id, kb_id = uuid.uuid4(), uuid.uuid4()
        db_mock = MagicMock()
        with patch("app.services.session_attachments.get_session_factory",
                   return_value=_factory_with(db_mock)):
            session_attachments.detach_kb(session_id, kb_id)
        db_mock.query.return_value.filter.return_value.delete.assert_called_once()
        db_mock.commit.assert_called()

    def test_detaching_something_not_attached_does_not_raise(self):
        session_id, kb_id = uuid.uuid4(), uuid.uuid4()
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.delete.return_value = 0
        with patch("app.services.session_attachments.get_session_factory",
                   return_value=_factory_with(db_mock)):
            session_attachments.detach_kb(session_id, kb_id)  # must not raise


class TestListAttachedKbIds:
    def test_returns_ids_oldest_first(self):
        session_id = uuid.uuid4()
        kb1, kb2 = uuid.uuid4(), uuid.uuid4()
        row1, row2 = MagicMock(knowledge_base_id=kb1), MagicMock(knowledge_base_id=kb2)
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.order_by.return_value.all.return_value = [row1, row2]
        with patch("app.services.session_attachments.get_session_factory",
                   return_value=_factory_with(db_mock)):
            result = session_attachments.list_attached_kb_ids(session_id)
        assert result == [str(kb1), str(kb2)]

    def test_empty_when_nothing_attached(self):
        session_id = uuid.uuid4()
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
        with patch("app.services.session_attachments.get_session_factory",
                   return_value=_factory_with(db_mock)):
            result = session_attachments.list_attached_kb_ids(session_id)
        assert result == []

    def test_empty_not_an_error_when_db_unavailable(self):
        with patch("app.services.session_attachments.get_session_factory", return_value=None):
            assert session_attachments.list_attached_kb_ids(uuid.uuid4()) == []

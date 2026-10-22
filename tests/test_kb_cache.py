"""
Tests for app/services/kb_cache.py - the cache that fixes the biggest
real-world slowness found while investigating "its very slow": chat.py was
looking up the selected knowledge base FRESH on every single chat message,
and knowledge_base.py re-fetched the whole active-KB list on every sidebar
load - both a live ~800ms-1s DB round trip in this project's environment,
paid on every call with zero caching.

DB-touching functions use a MagicMock session, same pattern as the rest of
this test suite. A module-level cache is shared state across tests, so an
autouse fixture resets it before/after every test.
"""
import uuid
import pytest
from unittest.mock import MagicMock, patch

import app.services.kb_cache as kb_cache
from app.services.kb_cache import get_active_kbs, get_kb_by_id, invalidate_kb_cache


@pytest.fixture(autouse=True)
def reset_kb_cache():
    invalidate_kb_cache()
    yield
    invalidate_kb_cache()


def _fake_kb(id_=None, name="Public Handbook", status="active", allowed_roles=None):
    kb = MagicMock()
    kb.id = id_ or uuid.uuid4()
    kb.name = name
    kb.description = "desc"
    kb.status = status
    kb.session_id = f"session-{kb.id}"
    kb.allowed_roles = allowed_roles
    return kb


class TestGetActiveKbs:
    def test_first_call_hits_db(self):
        kb = _fake_kb()
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.order_by.return_value.all.return_value = [kb]
        db_mock.close = MagicMock()
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.kb_cache.get_session_factory", return_value=factory):
            result = get_active_kbs()
        assert len(result) == 1
        assert result[0]["name"] == "Public Handbook"
        db_mock.query.assert_called_once()

    def test_second_call_within_ttl_does_not_hit_db(self):
        kb = _fake_kb()
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.order_by.return_value.all.return_value = [kb]
        db_mock.close = MagicMock()
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.kb_cache.get_session_factory", return_value=factory):
            get_active_kbs()
            get_active_kbs()
            get_active_kbs()
        db_mock.query.assert_called_once()

    def test_cache_expires_after_ttl(self):
        kb = _fake_kb()
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.order_by.return_value.all.return_value = [kb]
        db_mock.close = MagicMock()
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.kb_cache.get_session_factory", return_value=factory):
            get_active_kbs()
            with patch("app.services.kb_cache.time.monotonic", return_value=1e12):
                get_active_kbs()
        assert db_mock.query.call_count == 2

    def test_returned_list_is_a_copy_not_the_live_cache(self):
        kb = _fake_kb()
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.order_by.return_value.all.return_value = [kb]
        db_mock.close = MagicMock()
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.kb_cache.get_session_factory", return_value=factory):
            result = get_active_kbs()
            result.append({"id": "injected", "name": "fake"})
            result2 = get_active_kbs()
        assert len(result2) == 1

    def test_returns_plain_dicts_not_orm_instances(self):
        """Caching ORM instances past their session's lifetime risks
        DetachedInstanceError on later attribute access - must be plain
        dicts, safe to hold onto indefinitely."""
        kb = _fake_kb()
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.order_by.return_value.all.return_value = [kb]
        db_mock.close = MagicMock()
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.kb_cache.get_session_factory", return_value=factory):
            result = get_active_kbs()
        assert isinstance(result[0], dict)

    def test_db_unavailable_returns_empty_list(self):
        with patch("app.services.kb_cache.get_session_factory", return_value=None):
            assert get_active_kbs() == []


class TestGetKbById:
    def test_first_call_hits_db(self):
        kb = _fake_kb()
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.return_value = kb
        db_mock.close = MagicMock()
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.kb_cache.get_session_factory", return_value=factory):
            result = get_kb_by_id(kb.id)
        assert result["name"] == "Public Handbook"
        db_mock.query.assert_called_once()

    def test_second_call_within_ttl_does_not_hit_db(self):
        kb = _fake_kb()
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.return_value = kb
        db_mock.close = MagicMock()
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.kb_cache.get_session_factory", return_value=factory):
            get_kb_by_id(kb.id)
            get_kb_by_id(kb.id)
        db_mock.query.assert_called_once()

    def test_different_ids_each_hit_db_once(self):
        kb_a, kb_b = _fake_kb(name="A"), _fake_kb(name="B")
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.side_effect = [kb_a, kb_b]
        db_mock.close = MagicMock()
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.kb_cache.get_session_factory", return_value=factory):
            get_kb_by_id(kb_a.id)
            get_kb_by_id(kb_b.id)
        assert db_mock.query.call_count == 2

    def test_missing_kb_returns_none_and_caches_the_miss(self):
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.return_value = None
        db_mock.close = MagicMock()
        factory = MagicMock(return_value=db_mock)
        missing_id = uuid.uuid4()
        with patch("app.services.kb_cache.get_session_factory", return_value=factory):
            assert get_kb_by_id(missing_id) is None
            assert get_kb_by_id(missing_id) is None  # cached miss, no second DB hit
        db_mock.query.assert_called_once()

    def test_accepts_both_uuid_and_string(self):
        kb = _fake_kb()
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.return_value = kb
        db_mock.close = MagicMock()
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.kb_cache.get_session_factory", return_value=factory):
            get_kb_by_id(kb.id)           # uuid.UUID
            get_kb_by_id(str(kb.id))      # str - same cache key
        db_mock.query.assert_called_once()


class TestInvalidateKbCache:
    def test_invalidate_forces_active_list_refetch(self):
        kb = _fake_kb()
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.order_by.return_value.all.return_value = [kb]
        db_mock.close = MagicMock()
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.kb_cache.get_session_factory", return_value=factory):
            get_active_kbs()
            invalidate_kb_cache()
            get_active_kbs()
        assert db_mock.query.call_count == 2

    def test_invalidate_forces_by_id_refetch(self):
        kb = _fake_kb()
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.return_value = kb
        db_mock.close = MagicMock()
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.kb_cache.get_session_factory", return_value=factory):
            get_kb_by_id(kb.id)
            invalidate_kb_cache()
            get_kb_by_id(kb.id)
        assert db_mock.query.call_count == 2

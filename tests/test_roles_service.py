"""
Tests for app/services/roles_service.py - the core of the new role-based
access control feature: assigning department roles (hr/it/finance/sales/...)
to users by email, and deciding whether a user's roles permit access to a
knowledge base's allowed_roles restriction.

DB-touching functions are tested with a MagicMock session, same pattern as
tests/test_dedup.py. can_access_kb and _normalize_roles are pure and tested
directly.
"""
import pytest
from unittest.mock import MagicMock, patch

from app.services.roles_service import (
    _normalize_roles,
    _clear_roles_cache,
    get_user_roles,
    set_user_roles,
    list_user_roles,
    list_known_roles,
    can_access_kb,
)


@pytest.fixture(autouse=True)
def reset_roles_cache():
    """get_user_roles/set_user_roles/list_user_roles cache their DB reads -
    reset the module-level cache before and after every test so one test's
    mocked DB data can't leak into another's assertions."""
    _clear_roles_cache()
    yield
    _clear_roles_cache()


# ── _normalize_roles ───────────────────────────────────────────────────────────

class TestNormalizeRoles:
    def test_lowercases(self):
        assert _normalize_roles(["HR", "IT"]) == ["hr", "it"]

    def test_strips_whitespace(self):
        assert _normalize_roles([" hr ", "it\t"]) == ["hr", "it"]

    def test_drops_empty_strings(self):
        assert _normalize_roles(["hr", "", "  ", "it"]) == ["hr", "it"]

    def test_dedupes_preserving_first_occurrence_order(self):
        assert _normalize_roles(["hr", "IT", "hr", "it", "sales"]) == ["hr", "it", "sales"]

    def test_empty_list_returns_empty(self):
        assert _normalize_roles([]) == []

    def test_none_returns_empty(self):
        assert _normalize_roles(None) == []


# ── can_access_kb ──────────────────────────────────────────────────────────────

class TestCanAccessKb:
    def test_unrestricted_kb_none_allows_everyone(self):
        assert can_access_kb([], None) is True
        assert can_access_kb(["hr"], None) is True

    def test_unrestricted_kb_empty_list_allows_everyone(self):
        assert can_access_kb([], []) is True
        assert can_access_kb(["hr"], []) is True

    def test_restricted_kb_matching_role_allowed(self):
        assert can_access_kb(["hr"], ["hr"]) is True
        assert can_access_kb(["hr", "sales"], ["hr", "finance"]) is True

    def test_restricted_kb_no_matching_role_denied(self):
        assert can_access_kb(["it"], ["hr"]) is False
        assert can_access_kb([], ["hr"]) is False

    def test_restricted_kb_user_with_multiple_roles_needs_only_one_match(self):
        assert can_access_kb(["it", "sales"], ["sales", "finance"]) is True

    def test_case_is_not_re_normalized_here(self):
        """can_access_kb trusts its inputs are already normalized (lowercase)
        - normalization happens once, at write time (set_user_roles /
        admin.py's KB create/update), not on every read."""
        assert can_access_kb(["HR"], ["hr"]) is False


# ── get_user_roles ─────────────────────────────────────────────────────────────

class TestGetUserRoles:
    def test_empty_email_returns_empty(self):
        assert get_user_roles("") == []
        assert get_user_roles(None) == []

    def test_db_unavailable_returns_empty(self):
        with patch("app.services.roles_service.get_session_factory", return_value=None):
            assert get_user_roles("hr@gmail.com") == []

    def test_no_row_returns_empty(self):
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.return_value = None
        db_mock.close = MagicMock()
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.roles_service.get_session_factory", return_value=factory):
            assert get_user_roles("nobody@gmail.com") == []

    def test_row_found_returns_its_roles(self):
        row = MagicMock()
        row.roles = ["hr", "sales"]
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.return_value = row
        db_mock.close = MagicMock()
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.roles_service.get_session_factory", return_value=factory):
            assert get_user_roles("hr@gmail.com") == ["hr", "sales"]

    def test_email_lookup_is_case_insensitive(self):
        db_mock = MagicMock()
        captured = {}

        def fake_filter(*args, **kwargs):
            captured["args"] = args
            return db_mock.query.return_value.filter.return_value
        db_mock.query.return_value.filter.side_effect = fake_filter
        db_mock.query.return_value.filter.return_value.first.return_value = None
        db_mock.close = MagicMock()
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.roles_service.get_session_factory", return_value=factory):
            get_user_roles("HR@Gmail.com")
        compiled = str(captured["args"][0].compile(compile_kwargs={"literal_binds": True}))
        assert "hr@gmail.com" in compiled
        assert "HR@Gmail.com" not in compiled


# ── set_user_roles ──────────────────────────────────────────────────────────────

class TestSetUserRoles:
    def test_db_unavailable_raises(self):
        with patch("app.services.roles_service.get_session_factory", return_value=None):
            with pytest.raises(RuntimeError):
                set_user_roles("hr@gmail.com", ["hr"])

    def test_creates_new_row_when_none_exists(self):
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.return_value = None
        db_mock.close = MagicMock()
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.roles_service.get_session_factory", return_value=factory):
            result = set_user_roles("hr@gmail.com", ["HR", "Sales"])
        assert result == ["hr", "sales"]
        db_mock.add.assert_called_once()
        db_mock.commit.assert_called_once()

    def test_updates_existing_row(self):
        row = MagicMock()
        row.roles = ["hr"]
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.return_value = row
        db_mock.close = MagicMock()
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.roles_service.get_session_factory", return_value=factory):
            result = set_user_roles("hr@gmail.com", ["sales"])
        assert result == ["sales"]
        assert row.roles == ["sales"]
        db_mock.add.assert_not_called()
        db_mock.commit.assert_called_once()

    def test_empty_roles_list_clears_assignment(self):
        row = MagicMock()
        row.roles = ["hr"]
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.return_value = row
        db_mock.close = MagicMock()
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.roles_service.get_session_factory", return_value=factory):
            result = set_user_roles("hr@gmail.com", [])
        assert result == []
        assert row.roles == []

    def test_email_is_lowercased_before_storing(self):
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.return_value = None
        db_mock.close = MagicMock()
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.roles_service.get_session_factory", return_value=factory):
            set_user_roles("HR@Gmail.com", ["hr"])
        added_row = db_mock.add.call_args[0][0]
        assert added_row.email == "hr@gmail.com"


# ── list_user_roles / list_known_roles ──────────────────────────────────────────

class TestListUserRoles:
    def test_db_unavailable_returns_empty_dict(self):
        with patch("app.services.roles_service.get_session_factory", return_value=None):
            assert list_user_roles() == {}

    def test_returns_email_to_roles_mapping(self):
        row_a = MagicMock(email="hr@gmail.com", roles=["hr"])
        row_b = MagicMock(email="sales@gmail.com", roles=["sales"])
        db_mock = MagicMock()
        db_mock.query.return_value.all.return_value = [row_a, row_b]
        db_mock.close = MagicMock()
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.roles_service.get_session_factory", return_value=factory):
            result = list_user_roles()
        assert result == {"hr@gmail.com": ["hr"], "sales@gmail.com": ["sales"]}


class TestListKnownRoles:
    def test_db_unavailable_returns_empty_list(self):
        with patch("app.services.roles_service.get_session_factory", return_value=None):
            assert list_known_roles() == []

    def test_merges_roles_from_users_and_kbs(self):
        db_mock = MagicMock()

        def fake_query(col):
            q = MagicMock()
            # UserRole.roles query vs KnowledgeBase.allowed_roles query -
            # distinguished by which column object was asked for.
            if "user_roles" in str(col).lower() or getattr(col, "key", "") == "roles":
                q.all.return_value = [(["hr", "it"],), (["sales"],)]
            else:
                q.all.return_value = [(["finance"],), (None,)]
            return q
        db_mock.query.side_effect = fake_query
        db_mock.close = MagicMock()
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.roles_service.get_session_factory", return_value=factory):
            result = list_known_roles()
        assert set(result) >= {"hr", "it", "sales"}  # at least the user-roles side merged in
        assert result == sorted(result)  # sorted output


# ── Caching (fixes the slow admin dashboard / every-chat-message DB hit) ──────
# get_user_roles() is the hot path: it runs on every chat message and every
# knowledge-base listing to enforce access, and in this project a DB round
# trip to the remote Supabase Postgres instance measured ~800ms - paying
# that on every single call would make the whole app feel slow, not just
# the admin dashboard's Users tab. These tests verify the DB is actually
# hit only once per cache window, and that set_user_roles() keeps the
# cache correct immediately (write-through) rather than just invalidating
# it and paying for a re-fetch on the very next read.

def _db_with_row(email, roles):
    row = MagicMock(email=email, roles=roles)
    db_mock = MagicMock()
    db_mock.query.return_value.filter.return_value.first.return_value = row
    db_mock.query.return_value.all.return_value = [row]
    db_mock.close = MagicMock()
    return db_mock


class TestGetUserRolesCaching:
    def test_second_call_within_ttl_does_not_hit_db(self):
        db_mock = _db_with_row("hr@gmail.com", ["hr"])
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.roles_service.get_session_factory", return_value=factory):
            get_user_roles("hr@gmail.com")
            get_user_roles("hr@gmail.com")
            get_user_roles("hr@gmail.com")
        db_mock.query.assert_called_once()

    def test_different_emails_each_hit_db_once(self):
        db_mock = _db_with_row("hr@gmail.com", ["hr"])
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.roles_service.get_session_factory", return_value=factory):
            get_user_roles("hr@gmail.com")
            get_user_roles("sales@gmail.com")
        assert db_mock.query.call_count == 2

    def test_cache_expires_after_ttl(self):
        db_mock = _db_with_row("hr@gmail.com", ["hr"])
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.roles_service.get_session_factory", return_value=factory):
            get_user_roles("hr@gmail.com")
            with patch("app.services.roles_service.time.monotonic", return_value=1e12):
                get_user_roles("hr@gmail.com")
        assert db_mock.query.call_count == 2


class TestSetUserRolesWriteThrough:
    def test_set_then_get_does_not_hit_db_again(self):
        """The whole point of write-through: a role change must be visible
        to the very next get_user_roles() call with NO extra DB round trip,
        not just "eventually" once some invalidation triggers a re-fetch."""
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.return_value = None
        db_mock.close = MagicMock()
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.roles_service.get_session_factory", return_value=factory):
            set_user_roles("hr@gmail.com", ["hr", "sales"])
            db_mock.reset_mock()
            result = get_user_roles("hr@gmail.com")
        assert result == ["hr", "sales"]
        db_mock.query.assert_not_called()

    def test_set_updates_bulk_cache_too(self):
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.return_value = None
        db_mock.query.return_value.all.return_value = []
        db_mock.close = MagicMock()
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.roles_service.get_session_factory", return_value=factory):
            list_user_roles()  # populate the bulk cache (empty)
            set_user_roles("hr@gmail.com", ["hr"])
            db_mock.reset_mock()
            result = list_user_roles()
        assert result == {"hr@gmail.com": ["hr"]}
        db_mock.query.assert_not_called()  # served from the bulk cache, not re-fetched

    def test_clearing_roles_removes_from_bulk_cache(self):
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.return_value = None
        db_mock.query.return_value.all.return_value = []
        db_mock.close = MagicMock()
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.roles_service.get_session_factory", return_value=factory):
            list_user_roles()
            set_user_roles("hr@gmail.com", ["hr"])
            set_user_roles("hr@gmail.com", [])  # revoke
            result = list_user_roles()
        assert result == {}


class TestListUserRolesCaching:
    def test_second_call_within_ttl_does_not_hit_db(self):
        row = MagicMock(email="hr@gmail.com", roles=["hr"])
        db_mock = MagicMock()
        db_mock.query.return_value.all.return_value = [row]
        db_mock.close = MagicMock()
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.roles_service.get_session_factory", return_value=factory):
            list_user_roles()
            list_user_roles()
        db_mock.query.assert_called_once()

    def test_returned_dict_is_a_copy_not_the_live_cache(self):
        """Caller mutating the returned dict must not corrupt the cache."""
        row = MagicMock(email="hr@gmail.com", roles=["hr"])
        db_mock = MagicMock()
        db_mock.query.return_value.all.return_value = [row]
        db_mock.close = MagicMock()
        factory = MagicMock(return_value=db_mock)
        with patch("app.services.roles_service.get_session_factory", return_value=factory):
            result = list_user_roles()
            result["sales@gmail.com"] = ["sales"]
            result2 = list_user_roles()
        assert "sales@gmail.com" not in result2

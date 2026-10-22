"""
Tests for the Supabase admin-API helpers in app/services/auth_service.py,
specifically the caching added to fix the admin dashboard's Users tab being
slow: admin_list_users() used to make a live Supabase network call on every
single tab switch, with no caching, and was called directly (not via
asyncio.to_thread) from an async route - blocking the whole event loop for
the duration of that HTTP round-trip.

No real network calls - _supabase_admin() is mocked to return a fake client
whose .auth.admin methods are MagicMocks.
"""
import pytest
from unittest.mock import MagicMock, patch

import app.services.auth_service as auth_service
from app.services.auth_service import (
    admin_list_users,
    admin_create_user,
    admin_set_user_password,
    _invalidate_admin_users_cache,
)


def _fake_supabase_user(id_="u1", email="hr@gmail.com", name="HR", created_at=None):
    u = MagicMock()
    u.id = id_
    u.email = email
    u.user_metadata = {"name": name} if name else {}
    u.created_at = created_at
    return u


@pytest.fixture(autouse=True)
def reset_cache():
    """The cache is module-level state shared across tests - reset it
    before and after every test so none of these leak into each other."""
    _invalidate_admin_users_cache()
    yield
    _invalidate_admin_users_cache()


class TestAdminListUsersCaching:
    def test_first_call_hits_supabase(self):
        admin_client = MagicMock()
        admin_client.auth.admin.list_users.return_value = [_fake_supabase_user()]
        with patch.object(auth_service, "_supabase_admin", return_value=admin_client):
            result = admin_list_users()
        assert len(result) == 1
        assert result[0]["email"] == "hr@gmail.com"
        admin_client.auth.admin.list_users.assert_called_once()

    def test_second_call_within_ttl_uses_cache_not_supabase(self):
        admin_client = MagicMock()
        admin_client.auth.admin.list_users.return_value = [_fake_supabase_user()]
        with patch.object(auth_service, "_supabase_admin", return_value=admin_client):
            admin_list_users()
            admin_list_users()
            admin_list_users()
        # Only the first call should have actually reached "Supabase".
        admin_client.auth.admin.list_users.assert_called_once()

    def test_force_refresh_bypasses_cache(self):
        admin_client = MagicMock()
        admin_client.auth.admin.list_users.return_value = [_fake_supabase_user()]
        with patch.object(auth_service, "_supabase_admin", return_value=admin_client):
            admin_list_users()
            admin_list_users(force_refresh=True)
        assert admin_client.auth.admin.list_users.call_count == 2

    def test_cache_expires_after_ttl(self):
        admin_client = MagicMock()
        admin_client.auth.admin.list_users.return_value = [_fake_supabase_user()]
        with patch.object(auth_service, "_supabase_admin", return_value=admin_client):
            admin_list_users()
            # Simulate the TTL having elapsed.
            with patch("app.services.auth_service.time.monotonic", return_value=1e12):
                admin_list_users()
        assert admin_client.auth.admin.list_users.call_count == 2

    def test_invalidate_forces_next_call_to_refetch(self):
        admin_client = MagicMock()
        admin_client.auth.admin.list_users.return_value = [_fake_supabase_user()]
        with patch.object(auth_service, "_supabase_admin", return_value=admin_client):
            admin_list_users()
            _invalidate_admin_users_cache()
            admin_list_users()
        assert admin_client.auth.admin.list_users.call_count == 2

    def test_different_users_without_cache_pollution_across_tests(self):
        """Sanity check that the autouse reset_cache fixture actually works -
        a fresh call here must not see another test's cached data."""
        admin_client = MagicMock()
        admin_client.auth.admin.list_users.return_value = [_fake_supabase_user(email="fresh@gmail.com")]
        with patch.object(auth_service, "_supabase_admin", return_value=admin_client):
            result = admin_list_users()
        assert result[0]["email"] == "fresh@gmail.com"


class TestAdminCreateUserInvalidatesCache:
    def test_create_user_invalidates_cache(self):
        admin_client = MagicMock()
        admin_client.auth.admin.list_users.return_value = [_fake_supabase_user(email="old@gmail.com")]
        created_user = MagicMock()
        created_user.id = "new-id"
        created_user.email = "new@gmail.com"
        admin_client.auth.admin.create_user.return_value = MagicMock(user=created_user)

        with patch.object(auth_service, "_supabase_admin", return_value=admin_client):
            admin_list_users()  # populate cache with just "old@gmail.com"
            admin_create_user("new@gmail.com", "password123", "New")

            # list_users() right after create must see the invalidation -
            # the mock's return value doesn't actually include the new user
            # (it's a static MagicMock), but the point is it must have gone
            # back to Supabase for a fresh fetch, not served the stale cache.
            admin_list_users()

        assert admin_client.auth.admin.list_users.call_count == 2

    def test_create_user_duplicate_raises_value_error_without_touching_cache(self):
        admin_client = MagicMock()
        admin_client.auth.admin.create_user.side_effect = Exception("User already registered")
        with patch.object(auth_service, "_supabase_admin", return_value=admin_client):
            with pytest.raises(ValueError, match="already exists"):
                admin_create_user("dup@gmail.com", "password123")


class TestAdminSetUserPasswordUsesCachedList:
    def test_uses_admin_list_users_not_a_separate_raw_call(self):
        admin_client = MagicMock()
        admin_client.auth.admin.list_users.return_value = [_fake_supabase_user(id_="abc-123", email="hr@gmail.com")]
        with patch.object(auth_service, "_supabase_admin", return_value=admin_client):
            admin_set_user_password("hr@gmail.com", "newpassword123")
        admin_client.auth.admin.update_user_by_id.assert_called_once_with("abc-123", {"password": "newpassword123"})
        # Exactly one Supabase list call, not two (one from the old
        # implementation's own separate admin.list_users() call).
        assert admin_client.auth.admin.list_users.call_count == 1

    def test_unknown_email_raises(self):
        admin_client = MagicMock()
        admin_client.auth.admin.list_users.return_value = [_fake_supabase_user(email="hr@gmail.com")]
        with patch.object(auth_service, "_supabase_admin", return_value=admin_client):
            with pytest.raises(ValueError, match="No account found"):
                admin_set_user_password("nobody@gmail.com", "newpassword123")

    def test_email_match_is_case_insensitive(self):
        admin_client = MagicMock()
        admin_client.auth.admin.list_users.return_value = [_fake_supabase_user(id_="abc", email="HR@Gmail.com")]
        with patch.object(auth_service, "_supabase_admin", return_value=admin_client):
            admin_set_user_password("hr@gmail.com", "newpassword123")
        admin_client.auth.admin.update_user_by_id.assert_called_once_with("abc", {"password": "newpassword123"})

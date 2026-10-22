"""
Tests for the admin-side of role-based access control: persisting a
knowledge base's allowed_roles through create/update, and the new user
management endpoints (list/create/role-update) under app/routers/admin.py.

Supabase admin-API calls (auth_service.admin_list_users/admin_create_user)
are mocked - there is no local `users` table to query directly, so "list
users" always goes through Supabase; these tests never touch the network.
"""
import uuid
import pytest
from unittest.mock import MagicMock, patch
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


def _admin_client(router):
    app = FastAPI()

    @app.middleware("http")
    async def _inject_admin(request: Request, call_next):
        request.state.user = {"email": "admin@x.com", "role": "admin"}
        return await call_next(request)

    app.include_router(router)
    return TestClient(app)


def _db_patch(db_mock):
    session_factory_fn = MagicMock(return_value=db_mock)
    return patch("app.routers.admin.get_session_factory", return_value=session_factory_fn)


# ── KB create/update persist allowed_roles ─────────────────────────────────────

class TestKnowledgeBaseAllowedRoles:
    def test_create_persists_normalized_roles(self):
        from app.routers import admin as admin_mod

        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.return_value = None  # no name collision
        db_mock.close = MagicMock()
        client = _admin_client(admin_mod.router)

        with _db_patch(db_mock):
            res = client.post("/admin/knowledge-bases", json={
                "name": "HR Policies", "description": "desc",
                "allowed_roles": ["HR", " hr ", "Sales"],
            })
        assert res.status_code == 200
        added_kb = db_mock.add.call_args[0][0]
        assert added_kb.allowed_roles == ["hr", "sales"]
        assert res.json()["allowed_roles"] == ["hr", "sales"]

    def test_create_with_no_roles_is_unrestricted(self):
        from app.routers import admin as admin_mod

        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.return_value = None
        db_mock.close = MagicMock()
        client = _admin_client(admin_mod.router)

        with _db_patch(db_mock):
            res = client.post("/admin/knowledge-bases", json={"name": "Public Handbook"})
        assert res.status_code == 200
        added_kb = db_mock.add.call_args[0][0]
        assert added_kb.allowed_roles is None
        assert res.json()["allowed_roles"] == []

    def test_update_sets_allowed_roles(self):
        from app.routers import admin as admin_mod

        kb = MagicMock()
        kb.id = uuid.uuid4()
        kb.allowed_roles = None
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.return_value = kb
        db_mock.query.return_value.filter.return_value.count.return_value = 0
        db_mock.close = MagicMock()
        client = _admin_client(admin_mod.router)

        with _db_patch(db_mock):
            res = client.patch(f"/admin/knowledge-bases/{kb.id}", json={"allowed_roles": ["finance"]})
        assert res.status_code == 200
        assert kb.allowed_roles == ["finance"]

    def test_update_with_empty_list_clears_restriction(self):
        from app.routers import admin as admin_mod

        kb = MagicMock()
        kb.id = uuid.uuid4()
        kb.allowed_roles = ["hr"]
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.return_value = kb
        db_mock.query.return_value.filter.return_value.count.return_value = 0
        db_mock.close = MagicMock()
        client = _admin_client(admin_mod.router)

        with _db_patch(db_mock):
            res = client.patch(f"/admin/knowledge-bases/{kb.id}", json={"allowed_roles": []})
        assert res.status_code == 200
        assert kb.allowed_roles is None

    def test_update_omitting_allowed_roles_leaves_it_untouched(self):
        from app.routers import admin as admin_mod

        kb = MagicMock()
        kb.id = uuid.uuid4()
        kb.allowed_roles = ["hr"]
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.return_value = kb
        db_mock.query.return_value.filter.return_value.count.return_value = 0
        db_mock.close = MagicMock()
        client = _admin_client(admin_mod.router)

        with _db_patch(db_mock):
            res = client.patch(f"/admin/knowledge-bases/{kb.id}", json={"status": "inactive"})
        assert res.status_code == 200
        assert kb.allowed_roles == ["hr"]  # untouched


# ── GET /admin/users - merges Supabase list with local roles ─────────────────

class TestListUsers:
    def test_merges_supabase_users_with_local_roles(self):
        from app.routers import admin as admin_mod

        supabase_users = [
            {"id": "1", "email": "hr@gmail.com", "name": "HR", "created_at": "2026-01-01T00:00:00Z"},
            {"id": "2", "email": "nobody@gmail.com", "name": "Nobody", "created_at": "2026-01-02T00:00:00Z"},
        ]
        client = _admin_client(admin_mod.router)
        with patch("app.routers.admin.auth_service.admin_list_users", return_value=supabase_users), \
             patch("app.routers.admin.roles_service.list_user_roles", return_value={"hr@gmail.com": ["hr"]}):
            res = client.get("/admin/users")
        assert res.status_code == 200
        by_email = {u["email"]: u["roles"] for u in res.json()}
        assert by_email == {"hr@gmail.com": ["hr"], "nobody@gmail.com": []}

    def test_supabase_unreachable_returns_502(self):
        from app.routers import admin as admin_mod

        client = _admin_client(admin_mod.router)
        with patch("app.routers.admin.auth_service.admin_list_users", side_effect=Exception("network down")):
            res = client.get("/admin/users")
        assert res.status_code == 502

    def test_service_key_not_configured_returns_503(self):
        from app.routers import admin as admin_mod

        client = _admin_client(admin_mod.router)
        with patch("app.routers.admin.auth_service.admin_list_users", side_effect=RuntimeError("not configured")):
            res = client.get("/admin/users")
        assert res.status_code == 503


# ── POST /admin/users - create + assign initial roles ─────────────────────────

class TestCreateUser:
    def test_creates_and_assigns_roles(self):
        from app.routers import admin as admin_mod

        client = _admin_client(admin_mod.router)
        created = {"id": "123", "email": "newhire@gmail.com", "name": "New Hire"}
        with patch("app.routers.admin.auth_service.admin_create_user", return_value=created) as create_mock, \
             patch("app.routers.admin.roles_service.set_user_roles", return_value=["hr"]) as set_mock:
            res = client.post("/admin/users", json={
                "email": "newhire@gmail.com", "password": "supersecret1", "name": "New Hire", "roles": ["HR"],
            })
        assert res.status_code == 200
        assert res.json() == {**created, "roles": ["hr"]}
        create_mock.assert_called_once_with("newhire@gmail.com", "supersecret1", "New Hire")
        set_mock.assert_called_once_with("newhire@gmail.com", ["HR"])

    def test_rejects_short_password(self):
        from app.routers import admin as admin_mod
        client = _admin_client(admin_mod.router)
        res = client.post("/admin/users", json={"email": "x@gmail.com", "password": "short"})
        assert res.status_code == 400

    def test_rejects_invalid_email(self):
        from app.routers import admin as admin_mod
        client = _admin_client(admin_mod.router)
        res = client.post("/admin/users", json={"email": "not-an-email", "password": "supersecret1"})
        assert res.status_code == 400

    def test_duplicate_email_returns_400(self):
        from app.routers import admin as admin_mod
        client = _admin_client(admin_mod.router)
        with patch("app.routers.admin.auth_service.admin_create_user",
                   side_effect=ValueError("An account with this email already exists.")):
            res = client.post("/admin/users", json={"email": "dup@gmail.com", "password": "supersecret1"})
        assert res.status_code == 400


# ── PUT /admin/users/{email}/roles ────────────────────────────────────────────

class TestUpdateUserRoles:
    def test_replaces_roles(self):
        from app.routers import admin as admin_mod
        client = _admin_client(admin_mod.router)
        with patch("app.routers.admin.roles_service.set_user_roles", return_value=["sales"]) as set_mock:
            res = client.put("/admin/users/hr@gmail.com/roles", json={"roles": ["sales"]})
        assert res.status_code == 200
        assert res.json() == {"email": "hr@gmail.com", "roles": ["sales"]}
        set_mock.assert_called_once_with("hr@gmail.com", ["sales"])

    def test_db_unavailable_returns_503(self):
        from app.routers import admin as admin_mod
        client = _admin_client(admin_mod.router)
        with patch("app.routers.admin.roles_service.set_user_roles", side_effect=RuntimeError("Database unavailable")):
            res = client.put("/admin/users/hr@gmail.com/roles", json={"roles": ["sales"]})
        assert res.status_code == 503


# ── GET /admin/roles ───────────────────────────────────────────────────────────

class TestListRoles:
    def test_returns_known_roles(self):
        from app.routers import admin as admin_mod
        client = _admin_client(admin_mod.router)
        with patch("app.routers.admin.roles_service.list_known_roles", return_value=["hr", "it", "sales"]):
            res = client.get("/admin/roles")
        assert res.status_code == 200
        assert res.json() == ["hr", "it", "sales"]


# ── Admin gate still applies to every new endpoint ────────────────────────────

class TestAdminGateAppliesToNewEndpoints:
    def _non_admin_client(self, router):
        app = FastAPI()

        @app.middleware("http")
        async def _inject_user(request: Request, call_next):
            request.state.user = {"email": "u@x.com", "role": "user"}  # not admin
            return await call_next(request)

        app.include_router(router)
        return TestClient(app)

    @pytest.mark.parametrize("method,path,body", [
        ("get",   "/admin/users", None),
        ("post",  "/admin/users", {"email": "x@gmail.com", "password": "supersecret1"}),
        ("put",   "/admin/users/x@gmail.com/roles", {"roles": ["hr"]}),
        ("get",   "/admin/roles", None),
    ])
    def test_non_admin_user_gets_403(self, method, path, body):
        from app.routers import admin as admin_mod
        client = self._non_admin_client(admin_mod.router)
        res = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
        assert res.status_code == 403

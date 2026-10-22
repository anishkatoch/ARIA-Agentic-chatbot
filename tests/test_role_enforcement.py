"""
Tests for role-based access control actually being enforced at every point
a user can reach a knowledge base: the listing endpoint the chat UI's
attach panel uses (app.routers.knowledge_base), the attach endpoint itself
(which 404s on a KB the caller's roles don't permit, same as the listing),
and the chat endpoint (app.routers.chat) - which independently re-checks
access on every single chat turn against whatever knowledge bases are
currently attached, since roles can change after a KB was attached and a
client could otherwise bypass the filtered dropdown by writing a row some
other way.

Both routers read knowledge bases through app.services.kb_cache (a short-TTL
cache - see that module's docstring for why: an uncached lookup on every
single chat message measured ~800ms-1s against this project's remote
Postgres instance), so these tests patch kb_cache.get_active_kbs /
kb_cache.get_kb_by_id directly rather than the DB layer underneath it -
that's the actual integration seam the routers call through now.

A tiny stub auth middleware stands in for app.main's real one, injecting a
fixed request.state.user - the same mechanism the real middleware uses,
just without needing a real Supabase/admin token for every test case.
"""
import uuid
import pytest
from unittest.mock import MagicMock, patch
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


def _stub_user_app(router, user: dict | None):
    """Builds a standalone app with ONE router and a stub middleware that
    sets request.state.user, mirroring app.main.auth_middleware's effect
    without touching real auth."""
    app = FastAPI()

    @app.middleware("http")
    async def _inject_user(request: Request, call_next):
        request.state.user = user
        return await call_next(request)

    app.include_router(router)
    return TestClient(app)


def _kb(id_, name, allowed_roles=None, status="active"):
    """Matches the plain-dict shape app.services.kb_cache returns (not an
    ORM instance - the cache never holds onto ORM objects past their
    session, see that module's docstring)."""
    return {
        "id": str(id_), "name": name, "description": None, "status": status,
        "session_id": f"session-{id_}", "allowed_roles": allowed_roles,
    }


# ── GET /knowledge-bases - role-filtered listing ───────────────────────────────

class TestKnowledgeBaseListingFiltersByRole:
    def _make_client_with_kbs(self, kbs, user_roles):
        from app.routers import knowledge_base as kb_router_mod

        client = _stub_user_app(kb_router_mod.router, {"email": "u@x.com", "role": "user"})
        self._patches = [
            patch("app.routers.knowledge_base.kb_cache.get_active_kbs", return_value=kbs),
            patch("app.routers.knowledge_base.roles_service.get_user_roles", return_value=user_roles),
        ]
        for p in self._patches:
            p.start()
        return client

    def teardown_method(self):
        for p in getattr(self, "_patches", []):
            p.stop()

    def test_unrestricted_kb_visible_to_everyone(self):
        kbs = [_kb(uuid.uuid4(), "Public Handbook", allowed_roles=None)]
        client = self._make_client_with_kbs(kbs, user_roles=[])
        res = client.get("/knowledge-bases")
        assert res.status_code == 200
        assert len(res.json()) == 1

    def test_restricted_kb_hidden_from_user_without_role(self):
        kbs = [_kb(uuid.uuid4(), "HR Policies", allowed_roles=["hr"])]
        client = self._make_client_with_kbs(kbs, user_roles=["it"])
        res = client.get("/knowledge-bases")
        assert res.status_code == 200
        assert res.json() == []

    def test_restricted_kb_visible_to_user_with_matching_role(self):
        kbs = [_kb(uuid.uuid4(), "HR Policies", allowed_roles=["hr"])]
        client = self._make_client_with_kbs(kbs, user_roles=["hr"])
        res = client.get("/knowledge-bases")
        assert res.status_code == 200
        assert len(res.json()) == 1

    def test_mixed_kbs_only_permitted_ones_returned(self):
        public = _kb(uuid.uuid4(), "Public Handbook", allowed_roles=None)
        hr_only = _kb(uuid.uuid4(), "HR Policies", allowed_roles=["hr"])
        finance_only = _kb(uuid.uuid4(), "Finance Reports", allowed_roles=["finance"])
        client = self._make_client_with_kbs([public, hr_only, finance_only], user_roles=["hr"])
        res = client.get("/knowledge-bases")
        names = {row["name"] for row in res.json()}
        assert names == {"Public Handbook", "HR Policies"}

    def test_user_with_no_roles_only_sees_unrestricted(self):
        public = _kb(uuid.uuid4(), "Public Handbook", allowed_roles=None)
        hr_only = _kb(uuid.uuid4(), "HR Policies", allowed_roles=["hr"])
        client = self._make_client_with_kbs([public, hr_only], user_roles=[])
        res = client.get("/knowledge-bases")
        names = {row["name"] for row in res.json()}
        assert names == {"Public Handbook"}


# ── GET /knowledge-bases/{id}/files - same restriction, per-KB ────────────────

class TestKnowledgeBaseFilesRoleGate:
    def _make_client(self, kb, user_roles, docs=None):
        from app.routers import knowledge_base as kb_router_mod

        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.order_by.return_value.all.return_value = docs or []
        db_mock.close = MagicMock()
        session_factory_fn = MagicMock(return_value=db_mock)

        client = _stub_user_app(kb_router_mod.router, {"email": "u@x.com", "role": "user"})
        self._patches = [
            patch("app.routers.knowledge_base.kb_cache.get_kb_by_id", return_value=kb),
            patch("app.routers.knowledge_base.get_session_factory", return_value=session_factory_fn),
            patch("app.routers.knowledge_base.roles_service.get_user_roles", return_value=user_roles),
        ]
        for p in self._patches:
            p.start()
        return client

    def teardown_method(self):
        for p in getattr(self, "_patches", []):
            p.stop()

    def test_unauthorized_role_gets_404_not_403(self):
        """404, not 403 - doesn't confirm to an unauthorized caller that a
        restricted KB with this id exists at all."""
        kb = _kb(uuid.uuid4(), "HR Policies", allowed_roles=["hr"])
        client = self._make_client(kb, user_roles=["it"])
        res = client.get(f"/knowledge-bases/{kb['id']}/files")
        assert res.status_code == 404

    def test_unknown_kb_gets_404(self):
        client = self._make_client(None, user_roles=["hr"])
        res = client.get(f"/knowledge-bases/{uuid.uuid4()}/files")
        assert res.status_code == 404

    def test_authorized_role_succeeds(self):
        kb = _kb(uuid.uuid4(), "HR Policies", allowed_roles=["hr"])
        client = self._make_client(kb, user_roles=["hr"])
        res = client.get(f"/knowledge-bases/{kb['id']}/files")
        assert res.status_code == 200


# ── /sessions/{id}/knowledge-bases - attach/detach/list, same role gate ──────

class TestSessionAttachmentRoleGate:
    def _make_client(self, kb, user_roles):
        from app.routers import knowledge_base as kb_router_mod

        client = _stub_user_app(kb_router_mod.router, {"email": "u@x.com", "role": "user"})
        self._patches = [
            patch("app.routers.knowledge_base.kb_cache.get_kb_by_id", return_value=kb),
            patch("app.routers.knowledge_base.roles_service.get_user_roles", return_value=user_roles),
            patch("app.routers.knowledge_base.session_attachments.attach_kb"),
            patch("app.routers.knowledge_base.session_attachments.detach_kb"),
        ]
        for p in self._patches:
            p.start()
        return client

    def teardown_method(self):
        for p in getattr(self, "_patches", []):
            p.stop()

    def test_attach_restricted_kb_without_role_gets_404(self):
        kb = _kb(uuid.uuid4(), "HR Policies", allowed_roles=["hr"])
        client = self._make_client(kb, user_roles=["it"])
        res = client.post(f"/sessions/{uuid.uuid4()}/knowledge-bases/{kb['id']}")
        assert res.status_code == 404

    def test_attach_restricted_kb_with_matching_role_succeeds(self):
        from app.routers import knowledge_base as kb_router_mod

        kb = _kb(uuid.uuid4(), "HR Policies", allowed_roles=["hr"])
        client = self._make_client(kb, user_roles=["hr"])
        res = client.post(f"/sessions/{uuid.uuid4()}/knowledge-bases/{kb['id']}")
        assert res.status_code == 200
        kb_router_mod.session_attachments.attach_kb.assert_called_once()

    def test_attach_unknown_kb_gets_404(self):
        client = self._make_client(None, user_roles=["hr"])
        res = client.post(f"/sessions/{uuid.uuid4()}/knowledge-bases/{uuid.uuid4()}")
        assert res.status_code == 404

    def test_detach_needs_no_role_check(self):
        """Detaching is always allowed - removing an attachment can't leak
        anything the role gate protects, unlike attaching one."""
        client = self._make_client(None, user_roles=[])
        res = client.delete(f"/sessions/{uuid.uuid4()}/knowledge-bases/{uuid.uuid4()}")
        assert res.status_code == 200

    def test_list_attached_filters_by_current_role(self):
        """A KB attached earlier, then made role-restricted (or the user's
        roles changed) since, must not appear in the restored list."""
        from app.routers import knowledge_base as kb_router_mod

        kb = _kb(uuid.uuid4(), "HR Policies", allowed_roles=["hr"])
        client = self._make_client(kb, user_roles=["it"])
        with patch("app.routers.knowledge_base.session_attachments.list_attached_kb_ids",
                   return_value=[kb["id"]]):
            res = client.get(f"/sessions/{uuid.uuid4()}/knowledge-bases")
        assert res.status_code == 200
        assert res.json() == []

    def test_list_attached_returns_accessible_kbs(self):
        kb = _kb(uuid.uuid4(), "HR Policies", allowed_roles=["hr"])
        client = self._make_client(kb, user_roles=["hr"])
        with patch("app.routers.knowledge_base.session_attachments.list_attached_kb_ids",
                   return_value=[kb["id"]]):
            res = client.get(f"/sessions/{uuid.uuid4()}/knowledge-bases")
        assert res.status_code == 200
        assert res.json() == [{"id": kb["id"], "name": "HR Policies", "description": None}]


# ── POST /chat/ - re-checked independently of the listing AND attach endpoints ─
#
# Attachment is no longer a field in the chat request body - a session's
# attached knowledge bases live server-side (app.services.session_attachments)
# and /chat/ looks them up itself on every call via
# app.routers.chat._resolve_extra_sources, re-checking role access at read
# time (not just when the KB was originally attached) so a role change takes
# effect immediately, and so one stale/forbidden attachment never blocks the
# whole chat turn - it's just silently excluded from the sources queried.

class TestChatEndpointRoleGate:
    def _make_client(self, kb, user_roles, attached_ids=None):
        from app.routers import chat as chat_router_mod

        client = _stub_user_app(chat_router_mod.router, {"email": "u@x.com", "role": "user"})
        self._patches = [
            patch("app.routers.chat.kb_cache.get_kb_by_id", return_value=kb),
            patch("app.routers.chat.roles_service.get_user_roles", return_value=user_roles),
            patch("app.routers.chat.session_attachments.list_attached_kb_ids",
                  return_value=attached_ids if attached_ids is not None else ([kb["id"]] if kb else [])),
            patch("app.routers.chat.get_vector_store", return_value=MagicMock()),
        ]
        for p in self._patches:
            p.start()
        return client

    def teardown_method(self):
        for p in getattr(self, "_patches", []):
            p.stop()

    def test_restricted_kb_silently_excluded_without_the_personal_session_failing(self):
        """A session with a restricted KB attached, read by a user whose
        roles don't permit it: the chat turn still succeeds (against the
        personal session alone), it just never queries that KB - the actual
        bypass this closes is the KB's content leaking into the answer, not
        the whole request being allowed or denied wholesale."""
        kb = _kb(uuid.uuid4(), "HR Policies", allowed_roles=["hr"])
        client = self._make_client(kb, user_roles=["it"])
        with patch("app.routers.chat.answer_question") as mock_answer:
            mock_answer.return_value = ("answer text", 10, [])
            res = client.post("/chat/", json={
                "question": "what is in this", "session_id": str(uuid.uuid4()),
            })
        assert res.status_code == 200
        assert mock_answer.call_args.kwargs["extra_sources"] == []

    def test_authorized_role_is_allowed_through_to_answer_question(self):
        kb = _kb(uuid.uuid4(), "HR Policies", allowed_roles=["hr"])
        client = self._make_client(kb, user_roles=["hr"])
        with patch("app.routers.chat.answer_question", return_value=("answer text", 10, [])) as mock_answer:
            res = client.post("/chat/", json={
                "question": "what is in this", "session_id": str(uuid.uuid4()),
            })
        assert res.status_code == 200
        assert res.json()["answer"] == "answer text"
        extra = mock_answer.call_args.kwargs["extra_sources"]
        assert len(extra) == 1 and extra[0]["label"] == "HR Policies"

    def test_unrestricted_kb_allowed_for_any_role(self):
        kb = _kb(uuid.uuid4(), "Public Handbook", allowed_roles=None)
        client = self._make_client(kb, user_roles=[])
        with patch("app.routers.chat.answer_question", return_value=("answer text", 10, [])) as mock_answer:
            res = client.post("/chat/", json={
                "question": "what is in this", "session_id": str(uuid.uuid4()),
            })
        assert res.status_code == 200
        assert len(mock_answer.call_args.kwargs["extra_sources"]) == 1

    def test_unknown_attached_kb_id_silently_excluded(self):
        """A kb_id attached to the session that no longer resolves to an
        actual knowledge base (e.g. deleted after being attached) is
        dropped, not a 400 - the personal session is still answerable."""
        client = self._make_client(None, user_roles=["hr"], attached_ids=[str(uuid.uuid4())])
        with patch("app.routers.chat.answer_question", return_value=("answer text", 10, [])) as mock_answer:
            res = client.post("/chat/", json={
                "question": "what is in this", "session_id": str(uuid.uuid4()),
            })
        assert res.status_code == 200
        assert mock_answer.call_args.kwargs["extra_sources"] == []

    def test_inactive_attached_kb_silently_excluded(self):
        kb = _kb(uuid.uuid4(), "Archived", allowed_roles=None, status="inactive")
        client = self._make_client(kb, user_roles=[])
        with patch("app.routers.chat.answer_question", return_value=("answer text", 10, [])) as mock_answer:
            res = client.post("/chat/", json={
                "question": "what is in this", "session_id": str(uuid.uuid4()),
            })
        assert res.status_code == 200
        assert mock_answer.call_args.kwargs["extra_sources"] == []

    def test_no_knowledge_bases_attached_sends_empty_extra_sources(self):
        client = self._make_client(None, user_roles=[], attached_ids=[])
        with patch("app.routers.chat.answer_question", return_value=("answer text", 10, [])) as mock_answer:
            res = client.post("/chat/", json={
                "question": "what is in this", "session_id": str(uuid.uuid4()),
            })
        assert res.status_code == 200
        assert mock_answer.call_args.kwargs["extra_sources"] == []

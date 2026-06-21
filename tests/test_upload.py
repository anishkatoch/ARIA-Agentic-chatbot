"""
Tests for /upload/* endpoints.
External deps (parse_file, vector_store, dedup.check) are mocked
so tests run fast and offline.
"""
import json
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi.testclient import TestClient

from app.config import cfg


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_client():
    from app.routers import upload, chat
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(upload.router)
    app.include_router(chat.router)
    return TestClient(app)


def _pdf(name="test.pdf", size=1024):
    return ("files", (name, b"x" * size, "application/pdf"))


# ── File count validation ─────────────────────────────────────────────────────

class TestFileCountValidation:
    def test_too_many_files_returns_400(self):
        client = _make_client()
        files = [_pdf(f"file{i}.pdf") for i in range(cfg.max_files_per_session + 1)]
        res = client.post("/upload/files", files=files, headers={"X-Client-Token": "t1"})
        assert res.status_code == 400
        assert str(cfg.max_files_per_session) in res.json()["detail"]

    def test_exact_max_files_passes_validation(self):
        """Exact file count limit must not be rejected by the count check."""
        client = _make_client()
        # Use .txt files — no LiteParse needed, no extra mocking required
        files = [("files", (f"note{i}.txt", b"hello world", "text/plain"))
                 for i in range(cfg.max_files_per_session)]

        mock_store = MagicMock()
        mock_store.add_texts = MagicMock()

        with patch("app.routers.upload.get_vector_store", return_value=mock_store), \
             patch("app.services.dedup._get_db_session", return_value=None):
            res = client.post("/upload/files", files=files, headers={"X-Client-Token": "t1"})
        # 400 is only acceptable if NOT about file count
        if res.status_code == 400:
            assert "Maximum" not in res.json().get("detail", "")

    def test_one_file_is_allowed(self):
        client = _make_client()
        mock_store = MagicMock()
        mock_store.add_texts = MagicMock()

        with patch("app.routers.upload.get_vector_store", return_value=mock_store), \
             patch("app.services.dedup._get_db_session", return_value=None):
            res = client.post("/upload/files",
                              files=[("files", ("notes.txt", b"hello world", "text/plain"))],
                              headers={"X-Client-Token": "t1"})
        if res.status_code == 400:
            assert "Maximum" not in res.json().get("detail", "")


# ── Extension validation ──────────────────────────────────────────────────────

class TestExtensionValidation:
    def test_unsupported_extension_returns_400(self):
        client = _make_client()
        files = [("files", ("script.py", b"code", "text/plain"))]
        res = client.post("/upload/files", files=files, headers={"X-Client-Token": "t1"})
        assert res.status_code == 400
        assert "Unsupported" in res.json()["detail"] or "unsupported" in res.json()["detail"].lower()

    def test_exe_rejected(self):
        client = _make_client()
        files = [("files", ("malware.exe", b"bin", "application/octet-stream"))]
        res = client.post("/upload/files", files=files, headers={"X-Client-Token": "t1"})
        assert res.status_code == 400

    def test_pdf_accepted(self):
        """PDF extension must not be rejected by extension validation."""
        client = _make_client()
        from unittest.mock import AsyncMock as AM
        mock_parse = AM(return_value=("text", [{"page_number": 1, "start": 0, "end": 4, "confidence": None}]))
        mock_store = MagicMock()
        mock_store.add_texts = MagicMock()
        with patch("app.routers.upload.parse_file", mock_parse), \
             patch("app.routers.upload.get_vector_store", return_value=mock_store), \
             patch("app.services.dedup._get_db_session", return_value=None):
            res = client.post("/upload/files",
                              files=[("files", ("doc.pdf", b"fake-pdf", "application/pdf"))],
                              headers={"X-Client-Token": "t1"})
        if res.status_code == 400:
            assert "Unsupported" not in res.json().get("detail", "")

    def test_txt_accepted(self):
        """TXT extension must not be rejected — txt also doesn't need LiteParse."""
        client = _make_client()
        mock_store = MagicMock()
        mock_store.add_texts = MagicMock()
        with patch("app.routers.upload.get_vector_store", return_value=mock_store), \
             patch("app.services.dedup._get_db_session", return_value=None):
            res = client.post("/upload/files",
                              files=[("files", ("notes.txt", b"hello world content", "text/plain"))],
                              headers={"X-Client-Token": "t1"})
        if res.status_code == 400:
            assert "Unsupported" not in res.json().get("detail", "")

    def test_docx_accepted(self):
        """DOCX extension must pass the extension check."""
        client = _make_client()
        from unittest.mock import AsyncMock as AM
        mock_parse = AM(return_value=("text", [{"page_number": 1, "start": 0, "end": 4, "confidence": None}]))
        mock_store = MagicMock()
        mock_store.add_texts = MagicMock()
        with patch("app.routers.upload.parse_file", mock_parse), \
             patch("app.routers.upload.get_vector_store", return_value=mock_store), \
             patch("app.services.dedup._get_db_session", return_value=None):
            res = client.post("/upload/files",
                              files=[("files", ("report.docx", b"fake-doc", "application/vnd.openxmlformats"))],
                              headers={"X-Client-Token": "t1"})
        if res.status_code == 400:
            assert "Unsupported" not in res.json().get("detail", "")


# ── Per-file size validation ──────────────────────────────────────────────────

class TestFileSizeValidation:
    def test_file_over_per_file_limit_returns_400(self):
        client = _make_client()
        with patch.object(cfg, "max_file_size_mb", 1):
            big_content = b"x" * (2 * 1024 * 1024)  # 2MB > 1MB limit
            files = [("files", ("big.pdf", big_content, "application/pdf"))]
            res = client.post("/upload/files", files=files, headers={"X-Client-Token": "t1"})
        assert res.status_code == 400
        assert "exceeds" in res.json()["detail"]

    def test_total_size_over_session_limit_returns_400(self):
        client = _make_client()
        with patch.object(cfg, "max_session_size_mb", 1):
            # Two 600KB files = 1.2MB > 1MB session limit
            files = [
                ("files", (f"file{i}.pdf", b"x" * (600 * 1024), "application/pdf"))
                for i in range(2)
            ]
            res = client.post("/upload/files", files=files, headers={"X-Client-Token": "t1"})
        assert res.status_code == 400
        assert "session limit" in res.json()["detail"].lower() or "exceeds" in res.json()["detail"].lower()


# ── /upload/confirm endpoint ──────────────────────────────────────────────────

class TestConfirmEndpoint:
    def test_unknown_token_returns_404(self):
        client = _make_client()
        res = client.post("/upload/confirm",
                          json={"confirm_token": "does-not-exist", "action": "reuse"})
        assert res.status_code == 404

    def test_valid_reuse_action_returns_200(self):
        client = _make_client()
        from app.services.dedup import create_confirm_gate, _pending_confirms
        _pending_confirms.clear()
        create_confirm_gate("valid-tok-1")

        res = client.post("/upload/confirm",
                          json={"confirm_token": "valid-tok-1", "action": "reuse"})
        assert res.status_code == 200
        assert res.json()["status"] == "ok"

    def test_valid_reprocess_action_returns_200(self):
        client = _make_client()
        from app.services.dedup import create_confirm_gate, _pending_confirms
        _pending_confirms.clear()
        create_confirm_gate("valid-tok-2")

        res = client.post("/upload/confirm",
                          json={"confirm_token": "valid-tok-2", "action": "reprocess"})
        assert res.status_code == 200

    def test_invalid_action_value_returns_422(self):
        client = _make_client()
        res = client.post("/upload/confirm",
                          json={"confirm_token": "tok", "action": "delete"})
        assert res.status_code == 422  # Pydantic Literal validation

    def test_missing_token_returns_422(self):
        client = _make_client()
        res = client.post("/upload/confirm", json={"action": "reuse"})
        assert res.status_code == 422

    def test_second_confirm_same_token_returns_404(self):
        """resolve_confirm is one-shot — second POST on same token must 404."""
        client = _make_client()
        from app.services.dedup import create_confirm_gate, _pending_confirms, _resolved_actions
        _pending_confirms.clear()
        _resolved_actions.clear()
        create_confirm_gate("one-shot-tok")

        res1 = client.post("/upload/confirm",
                           json={"confirm_token": "one-shot-tok", "action": "reuse"})
        assert res1.status_code == 200
        res2 = client.post("/upload/confirm",
                           json={"confirm_token": "one-shot-tok", "action": "reuse"})
        assert res2.status_code == 404


# ── SSE stream: complete event contains session_id ────────────────────────────

class TestUploadSSEComplete:
    def test_complete_event_has_session_id(self):
        """
        A successful TXT upload SSE stream must end with a 'complete' event
        containing a non-empty session_id.
        """
        client = _make_client()
        mock_store = MagicMock()
        mock_store.add_texts = MagicMock()

        with patch("app.routers.upload.get_vector_store", return_value=mock_store), \
             patch("app.services.dedup._get_db_session", return_value=None):
            with client.stream("POST", "/upload/files",
                               files=[("files", ("notes.txt", b"hello world content here", "text/plain"))],
                               headers={"X-Client-Token": "stream-test"}) as response:
                events = _collect_sse_events(response)

        complete = [e for e in events if e.get("type") == "complete"]
        assert len(complete) == 1, f"Expected 1 complete event, got: {events}"
        assert "session_id" in complete[0]
        assert complete[0]["session_id"]


# ── Health check (sanity) ─────────────────────────────────────────────────────

class TestHealth:
    def test_health_endpoint(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()

        @app.get("/health")
        def health():
            return {"status": "ok"}

        with TestClient(app) as c:
            res = c.get("/health")
        assert res.status_code == 200
        assert res.json() == {"status": "ok"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _collect_sse_events(response) -> list[dict]:
    events = []
    for line in response.iter_lines():
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except Exception:
                pass
    return events

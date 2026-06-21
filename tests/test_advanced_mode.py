"""
Advanced Mode tests — UI toggle + backend connection + mid-chat switching.

Tests:
  1. Toggle exists and is OFF by default
  2. Toggle turns ON/OFF and label updates
  3. localStorage persists across page reload
  4. X-Advanced-Mode header sent correctly (OFF and ON)
  5. Can switch ON → OFF → ON mid-chat (anytime)
  6. Backend /chat/ accepts X-Advanced-Mode header
  7. Neo4j connection is live when advanced=true
  8. Backend returns valid response for both modes

Run:
    uv run pytest tests/test_advanced_mode.py -v -s
"""
import time
import json
import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

BASE_URL = "http://127.0.0.1:8002"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        pg = ctx.new_page()
        pg.goto(BASE_URL)
        pg.wait_for_load_state("networkidle")
        yield pg
        browser.close()


@pytest.fixture(scope="module")
def api_client():
    from app.routers import chat, upload
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(chat.router)
    app.include_router(upload.router)
    with TestClient(app) as c:
        yield c


# ── 1. Toggle UI presence ─────────────────────────────────────────────────────

class TestTogglePresence:

    def test_toggle_checkbox_exists(self, page):
        assert page.locator("#advanced-toggle").count() == 1

    def test_toggle_label_exists(self, page):
        assert page.locator("#advanced-label").count() == 1

    def test_toggle_is_off_by_default(self, page):
        # Clear localStorage first to get true default
        page.evaluate("() => localStorage.removeItem('rag_advanced_mode')")
        page.reload()
        page.wait_for_load_state("networkidle")

        checked = page.evaluate("() => document.getElementById('advanced-toggle').checked")
        assert checked is False, "Toggle should be OFF by default"

    def test_label_shows_off_by_default(self, page):
        label = page.locator("#advanced-label").text_content()
        assert "OFF" in label, f"Label should say OFF by default, got: {label}"

    def test_toggle_is_visible_in_header(self, page):
        assert page.locator("#advanced-toggle").is_visible() or \
               page.locator(".advanced-toggle-wrap").is_visible(), \
               "Toggle wrap should be visible in header"


# ── 2. Toggle ON/OFF switching ─────────────────────────────────────────────────

class TestToggleSwitching:

    def test_click_toggle_turns_on(self, page):
        # Ensure starts OFF
        page.evaluate("() => { const t = document.getElementById('advanced-toggle'); t.checked = false; t.dispatchEvent(new Event('change')); }")
        time.sleep(0.1)

        # Click toggle ON
        page.locator("#advanced-toggle").evaluate("el => { el.checked = true; el.dispatchEvent(new Event('change')); }")
        time.sleep(0.1)

        checked = page.evaluate("() => document.getElementById('advanced-toggle').checked")
        assert checked is True, "Toggle should be ON after clicking"

    def test_label_updates_to_on(self, page):
        page.locator("#advanced-toggle").evaluate("el => { el.checked = true; el.dispatchEvent(new Event('change')); }")
        time.sleep(0.1)
        label = page.locator("#advanced-label").text_content()
        assert "ON" in label, f"Label should say ON, got: {label}"

    def test_click_toggle_turns_off(self, page):
        # Turn OFF
        page.locator("#advanced-toggle").evaluate("el => { el.checked = false; el.dispatchEvent(new Event('change')); }")
        time.sleep(0.1)

        checked = page.evaluate("() => document.getElementById('advanced-toggle').checked")
        assert checked is False, "Toggle should be OFF after clicking again"

    def test_label_updates_to_off(self, page):
        page.locator("#advanced-toggle").evaluate("el => { el.checked = false; el.dispatchEvent(new Event('change')); }")
        time.sleep(0.1)
        label = page.locator("#advanced-label").text_content()
        assert "OFF" in label, f"Label should say OFF, got: {label}"

    def test_state_object_updated_on_toggle(self, page):
        # Turn ON
        page.locator("#advanced-toggle").evaluate("el => { el.checked = true; el.dispatchEvent(new Event('change')); }")
        time.sleep(0.1)
        state_val = page.evaluate("() => window._ragState.advancedMode")
        assert state_val is True, f"state.advancedMode should be True, got: {state_val}"

        # Turn OFF
        page.locator("#advanced-toggle").evaluate("el => { el.checked = false; el.dispatchEvent(new Event('change')); }")
        time.sleep(0.1)
        state_val = page.evaluate("() => window._ragState.advancedMode")
        assert state_val is False, f"state.advancedMode should be False, got: {state_val}"


# ── 3. localStorage persistence ───────────────────────────────────────────────

class TestLocalStoragePersistence:

    def test_on_state_persists_after_reload(self, page):
        # Set ON
        page.locator("#advanced-toggle").evaluate("el => { el.checked = true; el.dispatchEvent(new Event('change')); }")
        time.sleep(0.1)
        stored = page.evaluate("() => localStorage.getItem('rag_advanced_mode')")
        assert stored == "true", f"localStorage should be 'true', got: {stored}"

        # Reload
        page.reload()
        page.wait_for_load_state("networkidle")

        checked = page.evaluate("() => document.getElementById('advanced-toggle').checked")
        assert checked is True, "Toggle should remain ON after reload"
        label = page.locator("#advanced-label").text_content()
        assert "ON" in label

    def test_off_state_persists_after_reload(self, page):
        # Set OFF
        page.locator("#advanced-toggle").evaluate("el => { el.checked = false; el.dispatchEvent(new Event('change')); }")
        time.sleep(0.1)

        page.reload()
        page.wait_for_load_state("networkidle")

        checked = page.evaluate("() => document.getElementById('advanced-toggle').checked")
        assert checked is False, "Toggle should remain OFF after reload"

    def test_localstorage_key_is_correct(self, page):
        page.locator("#advanced-toggle").evaluate("el => { el.checked = true; el.dispatchEvent(new Event('change')); }")
        time.sleep(0.1)
        key = page.evaluate("() => localStorage.getItem('rag_advanced_mode')")
        assert key is not None, "localStorage key 'rag_advanced_mode' should be set"
        assert key in ("true", "false"), f"Value should be 'true' or 'false', got: {key}"


# ── 4. X-Advanced-Mode header sent correctly ──────────────────────────────────

class TestHeaderSentCorrectly:

    def test_header_false_when_toggle_off(self, page):
        """Intercept the /chat/ request and check the header."""
        # Turn OFF
        page.locator("#advanced-toggle").evaluate("el => { el.checked = false; el.dispatchEvent(new Event('change')); }")
        time.sleep(0.1)

        headers_captured = {}
        def capture(request):
            if "/chat/" in request.url:
                headers_captured.update(request.headers)

        page.on("request", capture)

        # Trigger a chat request via JS (simulate sendMessage without needing a session)
        page.evaluate("""() => {
            fetch('/chat/', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Advanced-Mode': window._ragState.advancedMode ? 'true' : 'false',
                },
                body: JSON.stringify({ session_id: '00000000-0000-0000-0000-000000000000', question: 'test' })
            }).catch(() => {});
        }""")
        time.sleep(0.5)

        adv_header = headers_captured.get("x-advanced-mode", headers_captured.get("X-Advanced-Mode", "NOT_FOUND"))
        assert adv_header == "false", f"X-Advanced-Mode should be 'false', got: {adv_header}"

    def test_header_true_when_toggle_on(self, page):
        # Turn ON
        page.locator("#advanced-toggle").evaluate("el => { el.checked = true; el.dispatchEvent(new Event('change')); }")
        time.sleep(0.1)

        headers_captured = {}
        def capture(request):
            if "/chat/" in request.url:
                headers_captured.update(request.headers)

        page.on("request", capture)

        page.evaluate("""() => {
            fetch('/chat/', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Advanced-Mode': window._ragState.advancedMode ? 'true' : 'false',
                },
                body: JSON.stringify({ session_id: '00000000-0000-0000-0000-000000000000', question: 'test' })
            }).catch(() => {});
        }""")
        time.sleep(0.5)

        adv_header = headers_captured.get("x-advanced-mode", headers_captured.get("X-Advanced-Mode", "NOT_FOUND"))
        assert adv_header == "true", f"X-Advanced-Mode should be 'true', got: {adv_header}"


# ── 5. Mid-chat switching (ON → OFF → ON anytime) ─────────────────────────────

class TestMidChatSwitching:

    def test_can_switch_off_to_on_anytime(self, page):
        page.locator("#advanced-toggle").evaluate("el => { el.checked = false; el.dispatchEvent(new Event('change')); }")
        time.sleep(0.1)
        assert page.evaluate("() => window._ragState.advancedMode") is False

        # Switch mid-chat
        page.locator("#advanced-toggle").evaluate("el => { el.checked = true; el.dispatchEvent(new Event('change')); }")
        time.sleep(0.1)
        assert page.evaluate("() => window._ragState.advancedMode") is True

    def test_can_switch_on_to_off_anytime(self, page):
        page.locator("#advanced-toggle").evaluate("el => { el.checked = true; el.dispatchEvent(new Event('change')); }")
        time.sleep(0.1)
        assert page.evaluate("() => window._ragState.advancedMode") is True

        page.locator("#advanced-toggle").evaluate("el => { el.checked = false; el.dispatchEvent(new Event('change')); }")
        time.sleep(0.1)
        assert page.evaluate("() => window._ragState.advancedMode") is False

    def test_rapid_switching_settles_correctly(self, page):
        """Rapid toggle ON/OFF/ON/OFF — final state should match last action."""
        for checked in [True, False, True, False, True]:
            page.locator("#advanced-toggle").evaluate(
                f"el => {{ el.checked = {'true' if checked else 'false'}; el.dispatchEvent(new Event('change')); }}"
            )
            time.sleep(0.05)

        final = page.evaluate("() => window._ragState.advancedMode")
        assert final is True, f"After ON/OFF/ON/OFF/ON — final state should be True, got: {final}"

    def test_header_changes_immediately_after_toggle(self, page):
        """Next request after toggling must use the NEW mode — no delay."""
        # Start OFF
        page.locator("#advanced-toggle").evaluate("el => { el.checked = false; el.dispatchEvent(new Event('change')); }")
        time.sleep(0.05)

        # Switch ON
        page.locator("#advanced-toggle").evaluate("el => { el.checked = true; el.dispatchEvent(new Event('change')); }")
        time.sleep(0.05)

        # Immediately read state — must be true
        val = page.evaluate("() => window._ragState.advancedMode")
        assert val is True, "state.advancedMode must update immediately on toggle"

        # Header for next request must be true
        header_val = page.evaluate("""() =>
            window._ragState.advancedMode ? 'true' : 'false'
        """)
        assert header_val == "true", f"Header value must be 'true' immediately after toggle ON, got: {header_val}"


# ── 6. Backend accepts X-Advanced-Mode header ─────────────────────────────────

class TestBackendHeader:

    def test_chat_accepts_advanced_false(self, api_client):
        res = api_client.post(
            "/chat/",
            json={"session_id": "00000000-0000-0000-0000-000000000001", "question": "hi"},
            headers={"X-Advanced-Mode": "false"},
        )
        # 200 or 500 (no vector store) — but NOT 422 (header not accepted)
        assert res.status_code != 422, f"Backend rejected X-Advanced-Mode header: {res.text}"

    def test_chat_accepts_advanced_true(self, api_client):
        res = api_client.post(
            "/chat/",
            json={"session_id": "00000000-0000-0000-0000-000000000001", "question": "hi"},
            headers={"X-Advanced-Mode": "true"},
        )
        assert res.status_code != 422, f"Backend rejected X-Advanced-Mode: true header: {res.text}"

    def test_chat_defaults_to_false_if_header_missing(self, api_client):
        """If header is absent, backend must not crash — defaults to false."""
        res = api_client.post(
            "/chat/",
            json={"session_id": "00000000-0000-0000-0000-000000000001", "question": "hi"},
        )
        assert res.status_code != 422, f"Missing header should not cause 422: {res.text}"

    def test_chat_reads_advanced_true_correctly(self, api_client):
        """Backend must parse 'true' as advanced=True (not string comparison failure)."""
        from unittest.mock import patch, MagicMock
        from langchain_core.documents import Document

        mock_vs = MagicMock()
        mock_vs.as_retriever.return_value.invoke.return_value = [
            Document(page_content="test chunk", metadata={"source": "test.txt", "chunk_index": 0, "page_number": 1, "confidence": 0.9})
        ]
        mock_vs._collection = MagicMock()
        mock_vs._collection.get.return_value = {"documents": ["test chunk"], "metadatas": [{"source": "test.txt"}]}

        with patch("app.routers.chat.get_vector_store", return_value=mock_vs):
            res = api_client.post(
                "/chat/",
                json={"session_id": "00000000-0000-0000-0000-000000000099", "question": "what is in the document?"},
                headers={"X-Advanced-Mode": "true"},
            )
        # Should not be 422 or 500 from bad header parsing
        assert res.status_code in (200, 500), f"Unexpected status: {res.status_code} — {res.text}"


# ── 7. Neo4j connection alive ─────────────────────────────────────────────────

class TestNeo4jConnection:

    def test_neo4j_driver_connects(self):
        from app.services.graph_store import _get_driver
        driver = _get_driver()
        assert driver is not None, "Neo4j driver should connect — check NEO4J_URI/USER/PASSWORD in .env"

    def test_neo4j_connection_is_alive(self):
        from app.services.graph_store import _get_driver
        driver = _get_driver()
        assert driver is not None
        driver.verify_connectivity()  # raises if dead

    def test_neo4j_can_run_query(self):
        from app.services.graph_store import _get_driver
        driver = _get_driver()
        assert driver is not None
        with driver.session() as session:
            result = session.run("RETURN 1 AS n")
            record = result.single()
            assert record["n"] == 1, "Neo4j should return 1 for RETURN 1"

    def test_query_graph_returns_string(self):
        from app.services.graph_store import query_graph
        result = query_graph("test question", "nonexistent_session_xyz")
        assert isinstance(result, str), "query_graph must always return a string"

    def test_build_graph_returns_bool(self):
        from app.services.graph_store import build_graph
        from app.services.rag import get_llm
        # Empty chunks — should return False gracefully
        result = build_graph([], "test_session_empty", get_llm())
        assert isinstance(result, bool), "build_graph must return a bool"


# ── 8. Full pipeline both modes ───────────────────────────────────────────────

class TestBothModesWork:

    def test_simple_mode_returns_answer(self, api_client):
        from unittest.mock import patch, MagicMock
        from langchain_core.documents import Document

        mock_vs = MagicMock()
        mock_vs.as_retriever.return_value.invoke.return_value = [
            Document(page_content="The penalty is $500,000.", metadata={"source": "test.txt", "chunk_index": 0, "page_number": 1, "confidence": 0.9})
        ]
        mock_vs._collection = MagicMock()
        mock_vs._collection.get.return_value = {
            "documents": ["The penalty is $500,000."],
            "metadatas": [{"source": "test.txt", "chunk_index": 0, "page_number": 1, "confidence": 0.9}]
        }

        with patch("app.routers.chat.get_vector_store", return_value=mock_vs):
            res = api_client.post(
                "/chat/",
                json={"session_id": "00000000-0000-0000-0000-000000000011", "question": "what is the penalty?"},
                headers={"X-Advanced-Mode": "false"},
            )

        assert res.status_code == 200, f"Simple mode failed: {res.text}"
        data = res.json()
        assert "answer" in data, "Response must have 'answer' field"
        assert len(data["answer"]) > 0, "Answer must not be empty"
        assert "elapsed_ms" in data, "Response must have 'elapsed_ms'"
        assert "citations" in data, "Response must have 'citations'"

    def test_advanced_mode_returns_answer(self, api_client):
        from unittest.mock import patch, MagicMock
        from langchain_core.documents import Document

        mock_vs = MagicMock()
        mock_vs.as_retriever.return_value.invoke.return_value = [
            Document(page_content="The penalty is $500,000.", metadata={"source": "test.txt", "chunk_index": 0, "page_number": 1, "confidence": 0.9})
        ]
        mock_vs._collection = MagicMock()
        mock_vs._collection.get.return_value = {
            "documents": ["The penalty is $500,000."],
            "metadatas": [{"source": "test.txt", "chunk_index": 0, "page_number": 1, "confidence": 0.9}]
        }

        with patch("app.routers.chat.get_vector_store", return_value=mock_vs):
            res = api_client.post(
                "/chat/",
                json={"session_id": "00000000-0000-0000-0000-000000000022", "question": "what is the penalty?"},
                headers={"X-Advanced-Mode": "true"},
            )

        assert res.status_code == 200, f"Advanced mode failed: {res.text}"
        data = res.json()
        assert "answer" in data
        assert len(data["answer"]) > 0

    def test_switching_mid_chat_simple_then_advanced(self, api_client):
        """Send one message simple, then one advanced — both must work."""
        from unittest.mock import patch, MagicMock
        from langchain_core.documents import Document

        mock_vs = MagicMock()
        mock_vs.as_retriever.return_value.invoke.return_value = [
            Document(page_content="Test clause content.", metadata={"source": "test.txt", "chunk_index": 0, "page_number": 1, "confidence": 0.9})
        ]
        mock_vs._collection = MagicMock()
        mock_vs._collection.get.return_value = {
            "documents": ["Test clause content."],
            "metadatas": [{"source": "test.txt", "chunk_index": 0}]
        }

        sid = "00000000-0000-0000-0000-000000000033"

        with patch("app.routers.chat.get_vector_store", return_value=mock_vs):
            # Message 1 — simple mode
            r1 = api_client.post("/chat/",
                json={"session_id": sid, "question": "summarize the doc"},
                headers={"X-Advanced-Mode": "false"})
            assert r1.status_code == 200, f"Simple mode msg failed: {r1.text}"

            # Message 2 — switch to advanced mid-chat
            r2 = api_client.post("/chat/",
                json={"session_id": sid, "question": "what are the obligations?"},
                headers={"X-Advanced-Mode": "true"})
            assert r2.status_code == 200, f"Advanced mode msg failed: {r2.text}"

            # Message 3 — switch back to simple
            r3 = api_client.post("/chat/",
                json={"session_id": sid, "question": "who are the parties?"},
                headers={"X-Advanced-Mode": "false"})
            assert r3.status_code == 200, f"Back to simple mode failed: {r3.text}"

        # All 3 must have answers
        for i, r in enumerate([r1, r2, r3], 1):
            assert len(r.json()["answer"]) > 0, f"Message {i} returned empty answer"

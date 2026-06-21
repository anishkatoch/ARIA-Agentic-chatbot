"""
Drag & Drop UI tests using Playwright.

Tests the drag-and-drop file upload feature at http://127.0.0.1:8002
Run with:
    uv run pytest tests/test_drag_drop.py -v -s
"""
import os
import time
import pytest
from playwright.sync_api import sync_playwright

BASE_URL = "http://127.0.0.1:8002"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page()
        pg.goto(BASE_URL)
        pg.wait_for_load_state("networkidle")
        yield pg
        browser.close()


@pytest.fixture(scope="module")
def sample_txt(tmp_path_factory):
    f = tmp_path_factory.mktemp("files") / "test_doc.txt"
    f.write_text(
        "This is a test document.\n"
        "Clause 1: The penalty for breach is $500,000 USD.\n"
        "Clause 2: The agreement lasts three years.\n"
    )
    return str(f)


@pytest.fixture(scope="module")
def sample_pdf(tmp_path_factory):
    f = tmp_path_factory.mktemp("files") / "test_doc.pdf"
    f.write_bytes(
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>endobj\n"
        b"xref\n0 4\n0000000000 65535 f\ntrailer<</Size 4/Root 1 0 R>>\n"
        b"startxref\n0\n%%EOF"
    )
    return str(f)


@pytest.fixture(scope="module")
def sample_invalid(tmp_path_factory):
    f = tmp_path_factory.mktemp("files") / "test.exe"
    f.write_bytes(b"MZ" + b"\x00" * 100)
    return str(f)


def reset_files(page):
    """Clear file list and state between tests."""
    page.evaluate("() => { window._ragState.files = []; document.getElementById('file-list').innerHTML = ''; }")
    time.sleep(0.1)


def drop_file_via_input(page, file_path: str):
    """Use Playwright's set_input_files — most reliable way to simulate file selection."""
    page.set_input_files("#file-input", file_path)
    time.sleep(0.3)


def dispatch_drag_event(page, event: str, with_file: str = None):
    """Dispatch a drag event on the drop zone, optionally with a file."""
    if with_file:
        with open(with_file, "rb") as f:
            content = list(f.read())
        fname = os.path.basename(with_file)
        mime = "text/plain" if with_file.endswith(".txt") else "application/pdf"
        page.evaluate("""([event, content, fname, mime]) => {
            const bytes = new Uint8Array(content);
            const file  = new File([bytes], fname, { type: mime });
            const dt    = new DataTransfer();
            dt.items.add(file);
            const zone  = document.getElementById('drop-zone');
            zone.dispatchEvent(new DragEvent(event, { bubbles: true, dataTransfer: dt }));
        }""", [event, content, fname, mime])
    else:
        page.evaluate("""(event) => {
            const dt   = new DataTransfer();
            const zone = document.getElementById('drop-zone');
            zone.dispatchEvent(new DragEvent(event, { bubbles: true, dataTransfer: dt }));
        }""", event)
    time.sleep(0.1)


# ── Tests: Drop zone presence ─────────────────────────────────────────────────

class TestDropZonePresence:

    def test_drop_zone_exists(self, page):
        assert page.locator("#drop-zone").count() == 1

    def test_drop_zone_visible(self, page):
        assert page.locator("#drop-zone").is_visible()

    def test_drop_zone_has_drag_drop_text(self, page):
        text = page.locator("#drop-zone .drop-title").text_content().lower()
        assert "drag" in text or "drop" in text

    def test_browse_button_visible(self, page):
        assert page.locator("#drop-zone .browse-btn").is_visible()

    def test_file_input_hidden(self, page):
        assert not page.locator("#file-input").is_visible()


# ── Tests: Visual feedback ────────────────────────────────────────────────────

class TestDragDropVisualFeedback:

    def test_dragenter_adds_dragover_class(self, page):
        page.evaluate("() => { document.getElementById('drop-zone').classList.remove('dragover'); }")
        # Must include 'Files' in dataTransfer.types for our handler to react
        page.evaluate("""() => {
            const dt = new DataTransfer();
            const f  = new File(['x'], 'test.txt', {type:'text/plain'});
            dt.items.add(f);
            document.dispatchEvent(new DragEvent('dragenter', { bubbles:true, cancelable:true, dataTransfer:dt }));
        }""")
        time.sleep(0.1)
        has_class = page.evaluate("() => document.getElementById('drop-zone').classList.contains('dragover')")
        assert has_class, "drop-zone should have .dragover class on dragenter with file"
        # Clean up
        page.evaluate("""() => {
            const dt = new DataTransfer();
            const f  = new File(['x'], 'test.txt', {type:'text/plain'});
            dt.items.add(f);
            document.dispatchEvent(new DragEvent('drop', { bubbles:true, cancelable:true, dataTransfer:dt }));
        }""")

    def test_dragover_class_removed_after_drop(self, page):
        dispatch_drag_event(page, "dragenter")
        dispatch_drag_event(page, "drop")
        has_class = page.evaluate("() => document.getElementById('drop-zone').classList.contains('dragover')")
        assert not has_class, "dragover class should be gone after drop"

    def test_dragleave_removes_class_when_counter_zero(self, page):
        """Enter once, leave once — counter hits 0 → class removed."""
        page.evaluate("() => { document.getElementById('drop-zone').classList.remove('dragover'); }")
        dispatch_drag_event(page, "dragenter")  # counter = 1
        dispatch_drag_event(page, "dragleave")  # counter = 0 → class removed
        has_class = page.evaluate("() => document.getElementById('drop-zone').classList.contains('dragover')")
        assert not has_class, "dragover class should be removed after dragleave brings counter to 0"

    def test_children_have_pointer_events_none(self, page):
        """Children must not block drag events from reaching the zone."""
        pe = page.evaluate("""() => ({
            icon:  window.getComputedStyle(document.querySelector('.drop-zone .drop-icon') || document.body).pointerEvents,
            title: window.getComputedStyle(document.querySelector('.drop-zone .drop-title') || document.body).pointerEvents,
            sub:   window.getComputedStyle(document.querySelector('.drop-zone .drop-sub') || document.body).pointerEvents,
        })""")
        assert pe["icon"]  == "none", f"drop-icon should be pointer-events:none, got {pe['icon']}"
        assert pe["title"] == "none", f"drop-title should be pointer-events:none, got {pe['title']}"
        assert pe["sub"]   == "none", f"drop-sub should be pointer-events:none, got {pe['sub']}"

    def test_browse_btn_pointer_events_auto(self, page):
        """Browse button must still be clickable."""
        pe = page.evaluate("""() =>
            window.getComputedStyle(document.querySelector('.drop-zone .browse-btn')).pointerEvents
        """)
        assert pe == "auto", f"browse-btn should be pointer-events:auto, got {pe}"


# ── Tests: File drop functionality ────────────────────────────────────────────

class TestFileDropFunctionality:

    def test_txt_file_appears_in_list(self, page, sample_txt):
        reset_files(page)
        drop_file_via_input(page, sample_txt)

        chips = page.locator("#file-list .file-chip")
        assert chips.count() > 0, "File chip should appear after drop"
        name = page.locator("#file-list .file-chip-name").first.text_content()
        assert "test_doc.txt" in name

    def test_file_size_displayed(self, page, sample_txt):
        reset_files(page)
        drop_file_via_input(page, sample_txt)

        size = page.locator("#file-list .file-chip-size").first.text_content()
        assert any(u in size for u in ["B", "KB", "MB"]), f"Size should show unit, got: {size}"

    def test_file_type_badge_shown(self, page, sample_txt):
        reset_files(page)
        drop_file_via_input(page, sample_txt)

        badge = page.locator("#file-list .file-chip-icon").first.text_content().strip()
        assert badge == "TXT", f"Badge should say TXT, got: {badge}"

    def test_pdf_file_accepted(self, page, sample_pdf):
        reset_files(page)
        drop_file_via_input(page, sample_pdf)

        assert page.locator("#file-list .file-chip").count() > 0
        badge = page.locator("#file-list .file-chip-icon").first.text_content().strip()
        assert badge == "PDF"

    def test_remove_button_removes_file(self, page, sample_txt):
        reset_files(page)
        drop_file_via_input(page, sample_txt)
        assert page.locator("#file-list .file-chip").count() == 1

        page.locator("#file-list .file-chip-remove").first.click()
        time.sleep(0.2)
        assert page.locator("#file-list .file-chip").count() == 0

    def test_invalid_file_type_rejected(self, page, sample_invalid):
        reset_files(page)
        drop_file_via_input(page, sample_invalid)

        assert page.locator("#file-list .file-chip").count() == 0, "Invalid file should be rejected"
        assert page.locator(".toast.error").count() > 0, "Error toast should appear"

    def test_multiple_files_added(self, page, sample_txt, sample_pdf):
        reset_files(page)
        drop_file_via_input(page, sample_txt)
        drop_file_via_input(page, sample_pdf)

        assert page.locator("#file-list .file-chip").count() == 2

    def test_session_size_bar_appears(self, page, sample_txt):
        reset_files(page)
        drop_file_via_input(page, sample_txt)

        assert page.locator(".session-size-bar").count() > 0


# ── Tests: Actual drag events ─────────────────────────────────────────────────

class TestDragEventsViaJS:

    def test_drop_event_with_file_adds_to_list(self, page, sample_txt):
        reset_files(page)
        dispatch_drag_event(page, "dragenter", sample_txt)
        dispatch_drag_event(page, "dragover",  sample_txt)
        dispatch_drag_event(page, "drop",      sample_txt)
        time.sleep(0.3)

        chips = page.locator("#file-list .file-chip").count()
        assert chips > 0, "Drop via DataTransfer should add file to list"

    def test_dragover_class_cleared_after_drop(self, page, sample_txt):
        dispatch_drag_event(page, "dragenter", sample_txt)
        dispatch_drag_event(page, "drop",      sample_txt)

        has_class = page.evaluate("() => document.getElementById('drop-zone').classList.contains('dragover')")
        assert not has_class, "dragover class must be gone after drop"

    def test_state_files_updated_after_drop(self, page, sample_txt):
        reset_files(page)
        dispatch_drag_event(page, "dragenter", sample_txt)
        dispatch_drag_event(page, "dragover",  sample_txt)
        dispatch_drag_event(page, "drop",      sample_txt)
        time.sleep(0.3)

        count = page.evaluate("() => window._ragState.files.length")
        assert count > 0, f"window._ragState.files should have entries, got {count}"

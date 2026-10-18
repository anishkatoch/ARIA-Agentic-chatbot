"""
Tests for app/services/ingestion.py.
TXT parsing is tested without LiteParse (no binary dependency needed).
PDF/DOC parsing is smoke-tested only if LiteParse is available.
"""
import pytest
from unittest.mock import patch, AsyncMock


# ── TXT parsing (no external deps) ───────────────────────────────────────────

class TestParseTxt:
    async def test_plain_text_returns_text(self):
        from app.services.ingestion import parse_file
        text, spans = await parse_file(b"hello world", "readme.txt")
        assert text == "hello world"

    async def test_txt_single_span(self):
        from app.services.ingestion import parse_file
        text, spans = await parse_file(b"test content", "doc.txt")
        assert len(spans) == 1

    async def test_txt_span_page_number_is_1(self):
        from app.services.ingestion import parse_file
        _, spans = await parse_file(b"sample", "file.TXT")
        assert spans[0]["page_number"] == 1

    async def test_txt_span_covers_full_text(self):
        from app.services.ingestion import parse_file
        content = b"abcdefghij"
        text, spans = await parse_file(content, "test.txt")
        assert spans[0]["start"] == 0
        assert spans[0]["end"] == len(text)

    async def test_txt_confidence_is_none(self):
        from app.services.ingestion import parse_file
        _, spans = await parse_file(b"text", "notes.txt")
        assert spans[0]["confidence"] is None

    async def test_txt_utf8_decoding(self):
        from app.services.ingestion import parse_file
        content = "héllo wörld".encode("utf-8")
        text, _ = await parse_file(content, "unicode.txt")
        assert "héllo" in text

    async def test_txt_invalid_bytes_ignored(self):
        from app.services.ingestion import parse_file
        bad_bytes = b"valid text \xff\xfe more text"
        text, _ = await parse_file(bad_bytes, "corrupt.txt")
        assert "valid text" in text
        assert "more text" in text

    async def test_empty_txt(self):
        from app.services.ingestion import parse_file
        text, spans = await parse_file(b"", "empty.txt")
        assert text == ""
        assert spans[0]["start"] == 0
        assert spans[0]["end"] == 0

    async def test_case_insensitive_extension(self):
        from app.services.ingestion import parse_file
        text, spans = await parse_file(b"content", "README.TXT")
        assert text == "content"
        assert len(spans) == 1


# ── JSON flattening ───────────────────────────────────────────────────────────

class TestFlattenJson:
    def test_dict_values_joined(self):
        from app.services.ingestion import _flatten_json
        result = _flatten_json({"a": "hello", "b": "world"})
        assert "hello" in result
        assert "world" in result

    def test_nested_dict(self):
        from app.services.ingestion import _flatten_json
        result = _flatten_json({"outer": {"inner": "value"}})
        assert "value" in result

    def test_list_items_joined(self):
        from app.services.ingestion import _flatten_json
        result = _flatten_json(["apple", "banana", "cherry"])
        assert "apple" in result
        assert "cherry" in result

    def test_string_passthrough(self):
        from app.services.ingestion import _flatten_json
        assert _flatten_json("hello") == "hello"

    def test_number_converted(self):
        from app.services.ingestion import _flatten_json
        result = _flatten_json({"count": 42})
        assert "42" in result

    def test_empty_dict(self):
        from app.services.ingestion import _flatten_json
        result = _flatten_json({})
        assert result == ""

    def test_mixed_nested(self):
        from app.services.ingestion import _flatten_json
        data = {"items": [{"name": "Alice"}, {"name": "Bob"}], "total": 2}
        result = _flatten_json(data)
        assert "Alice" in result
        assert "Bob" in result


# ── avg_confidence helper ─────────────────────────────────────────────────────

class TestAvgConfidence:
    def test_all_scores_present(self):
        from app.services.ingestion import _avg_confidence
        page = _mock_page([0.9, 0.8, 0.7])
        result = _avg_confidence(page)
        assert result == pytest.approx(0.8, abs=0.001)

    def test_none_scores_skipped(self):
        from app.services.ingestion import _avg_confidence
        page = _mock_page([0.9, None, 0.7])
        result = _avg_confidence(page)
        assert result == pytest.approx(0.8, abs=0.001)

    def test_all_none_returns_none(self):
        from app.services.ingestion import _avg_confidence
        page = _mock_page([None, None])
        assert _avg_confidence(page) is None

    def test_empty_items(self):
        from app.services.ingestion import _avg_confidence
        page = _mock_page([])
        assert _avg_confidence(page) is None

    def test_rounded_to_4_decimal_places(self):
        from app.services.ingestion import _avg_confidence
        page = _mock_page([0.123456789])
        result = _avg_confidence(page)
        assert result == 0.1235  # rounded to 4dp


def _mock_page(confidence_values: list):
    """Build a fake LiteParse page object with text_items."""
    class Item:
        def __init__(self, c):
            self.confidence = c
    class Page:
        text_items = [Item(c) for c in confidence_values]
    return Page()


# ── Docling fallback ──────────────────────────────────────────────────────────

class TestDoclingFallback:
    async def test_falls_back_to_docling_when_liteparse_raises(self):
        import app.services.ingestion as ingestion
        with patch.object(ingestion, "_parse_with_liteparse", side_effect=RuntimeError("liteparse boom")), \
             patch.object(ingestion, "_parse_with_docling", return_value=("docling text", [{"page_number": 1, "start": 0, "end": 12, "confidence": 0.9}])) as mock_docling:
            text, spans = await ingestion.parse_file(b"%PDF-1.4 fake", "doc.pdf")
        assert text == "docling text"
        assert spans[0]["confidence"] == 0.9
        mock_docling.assert_called_once()

    async def test_liteparse_success_never_calls_docling(self):
        import app.services.ingestion as ingestion
        with patch.object(ingestion, "_parse_with_liteparse", return_value=("liteparse text", [{"page_number": 1, "start": 0, "end": 14, "confidence": 0.99}])), \
             patch.object(ingestion, "_parse_with_docling") as mock_docling:
            text, spans = await ingestion.parse_file(b"%PDF-1.4 fake", "doc.pdf")
        assert text == "liteparse text"
        mock_docling.assert_not_called()

    async def test_raises_when_both_parsers_fail(self):
        import app.services.ingestion as ingestion
        with patch.object(ingestion, "_parse_with_liteparse", side_effect=RuntimeError("liteparse boom")), \
             patch.object(ingestion, "_parse_with_docling", side_effect=RuntimeError("docling boom")):
            with pytest.raises(RuntimeError) as exc_info:
                await ingestion.parse_file(b"%PDF-1.4 fake", "doc.pdf")
        assert "doc.pdf" in str(exc_info.value)
        assert "docling boom" in str(exc_info.value)

    async def test_docling_fallback_cleans_up_tmp_file(self):
        import app.services.ingestion as ingestion
        with patch.object(ingestion, "_parse_with_liteparse", side_effect=RuntimeError("boom")), \
             patch.object(ingestion, "_parse_with_docling", side_effect=RuntimeError("boom too")):
            with pytest.raises(RuntimeError):
                await ingestion.parse_file(b"%PDF-1.4 fake", "doc.pdf")
        # No leaked temp files from this parse attempt
        import tempfile, glob, os
        leaked = glob.glob(os.path.join(tempfile.gettempdir(), "tmp*.pdf"))
        assert not any(os.path.exists(f) and os.path.getsize(f) == len(b"%PDF-1.4 fake") for f in leaked)


# ── _docling_page_confidence helper ───────────────────────────────────────────

class TestDoclingPageConfidence:
    def test_valid_score_rounded(self):
        from app.services.ingestion import _docling_page_confidence
        class Score:
            mean_score = 0.123456
        class Report:
            pages = {1: Score()}
        assert _docling_page_confidence(Report(), 1) == 0.1235

    def test_missing_page_returns_none(self):
        from app.services.ingestion import _docling_page_confidence
        class Report:
            pages = {}
        assert _docling_page_confidence(Report(), 1) is None

    def test_nan_score_returns_none(self):
        from app.services.ingestion import _docling_page_confidence
        class Score:
            mean_score = float("nan")
        class Report:
            pages = {1: Score()}
        assert _docling_page_confidence(Report(), 1) is None

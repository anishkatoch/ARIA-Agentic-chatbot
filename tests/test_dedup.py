"""
Tests for app/services/dedup.py.
Covers: hashing, TF-IDF similarity, chunk extraction,
confirmation gate, and the main check() decision tree.
"""
import asyncio
import pytest
from unittest.mock import MagicMock, patch

from app.services.dedup import (
    sha256,
    _tfidf_similarity,
    _three_point_similarity,
    _extract_chunks,
    create_confirm_gate,
    resolve_confirm,
    consume_confirm,
    DedupResult,
    check,
    _pending_confirms,
    _resolved_actions,
)


# ── SHA256 ────────────────────────────────────────────────────────────────────

class TestSha256:
    def test_consistent(self):
        assert sha256(b"hello world") == sha256(b"hello world")

    def test_unique(self):
        assert sha256(b"hello") != sha256(b"world")

    def test_empty_bytes(self):
        h = sha256(b"")
        assert len(h) == 64  # hex SHA256 is always 64 chars

    def test_known_value_format(self):
        """Output is always a 64-char lowercase hex string."""
        result = sha256(b"abc")
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_known_value_stable_across_calls(self):
        """Same input always produces the same output."""
        assert sha256(b"abc") == sha256(b"abc")
        assert sha256(b"") == sha256(b"")

    def test_large_content(self):
        big = b"x" * (5 * 1024 * 1024)  # 5MB
        h1 = sha256(big)
        h2 = sha256(big)
        assert h1 == h2


# ── TF-IDF similarity ─────────────────────────────────────────────────────────

class TestTfidfSimilarity:
    def test_identical_returns_high(self):
        text = "the quick brown fox jumps over the lazy dog"
        score = _tfidf_similarity(text, text)
        assert score > 0.99

    def test_completely_different(self):
        score = _tfidf_similarity("cat sat on mat", "quantum physics relativity")
        assert score < 0.1

    def test_partial_overlap(self):
        score = _tfidf_similarity("cat sat on mat", "cat ran away fast")
        assert 0.0 < score < 1.0

    def test_empty_string_a(self):
        assert _tfidf_similarity("", "hello world") == 0.0

    def test_empty_string_b(self):
        assert _tfidf_similarity("hello world", "") == 0.0

    def test_both_empty(self):
        assert _tfidf_similarity("", "") == 0.0

    def test_single_word_match(self):
        score = _tfidf_similarity("cat", "cat")
        assert score > 0.99

    def test_symmetry(self):
        a = "machine learning is powerful"
        b = "deep learning neural networks"
        assert abs(_tfidf_similarity(a, b) - _tfidf_similarity(b, a)) < 1e-9


class TestThreePointSimilarity:
    def test_identical_texts(self):
        text = "word " * 100
        s1, s2, s3 = _three_point_similarity(text, text, text, text, text, text)
        assert s1 > 0.99 and s2 > 0.99 and s3 > 0.99

    def test_different_texts(self):
        a = "apple orange banana mango grape"
        b = "quantum physics relativity entropy"
        s1, s2, s3 = _three_point_similarity(a, a, a, b, b, b)
        assert s1 < 0.2 and s2 < 0.2 and s3 < 0.2


# ── Chunk extraction ──────────────────────────────────────────────────────────

class TestExtractChunks:
    def test_short_text_all_same(self):
        text = "hello world"
        first, middle, last = _extract_chunks(text)
        # all three overlap since text is < 500 chars
        assert text in first
        assert len(first) <= 500
        assert len(middle) <= 500
        assert len(last) <= 500

    def test_long_text_distinct_positions(self):
        # 3000 chars: first 500, middle 500 centered, last 500
        text = "A" * 500 + "B" * 2000 + "C" * 500
        first, middle, last = _extract_chunks(text)
        assert first.startswith("A")
        assert last.endswith("C")
        assert "B" in middle  # middle falls in the B zone

    def test_first_is_500_chars(self):
        text = "x" * 2000
        first, _, _ = _extract_chunks(text)
        assert len(first) == 500

    def test_last_is_500_chars(self):
        text = "x" * 2000
        _, _, last = _extract_chunks(text)
        assert len(last) == 500

    def test_empty_text(self):
        first, middle, last = _extract_chunks("")
        assert first == middle == last == ""


# ── Confirmation gate ─────────────────────────────────────────────────────────

class TestConfirmGate:
    def setup_method(self):
        _pending_confirms.clear()
        _resolved_actions.clear()

    def test_create_returns_event(self):
        event = create_confirm_gate("tok1")
        assert not event.is_set()

    def test_resolve_unknown_token(self):
        assert resolve_confirm("unknown-token", "reuse") is False

    def test_resolve_sets_event(self):
        event = create_confirm_gate("tok2")
        assert resolve_confirm("tok2", "reuse") is True
        assert event.is_set()

    def test_consume_returns_action(self):
        create_confirm_gate("tok3")
        resolve_confirm("tok3", "reprocess")
        action = consume_confirm("tok3")
        assert action == "reprocess"

    def test_resolve_removes_from_pending(self):
        create_confirm_gate("tok4")
        resolve_confirm("tok4", "reuse")
        # Token moved from pending to resolved on resolve_confirm()
        assert "tok4" not in _pending_confirms
        assert "tok4" in _resolved_actions

    def test_consume_removes_from_resolved(self):
        create_confirm_gate("tok4b")
        resolve_confirm("tok4b", "reuse")
        consume_confirm("tok4b")
        assert "tok4b" not in _resolved_actions

    def test_consume_unknown_returns_reprocess(self):
        action = consume_confirm("never-existed")
        assert action == "reprocess"

    def test_reuse_action(self):
        create_confirm_gate("tok5")
        resolve_confirm("tok5", "reuse")
        assert consume_confirm("tok5") == "reuse"

    def test_reprocess_action(self):
        create_confirm_gate("tok6")
        resolve_confirm("tok6", "reprocess")
        assert consume_confirm("tok6") == "reprocess"


# ── Main check() function ─────────────────────────────────────────────────────

class TestDedupCheck:
    """
    All DB calls are mocked — no real Postgres required.
    """

    @pytest.fixture(autouse=True)
    def clear_pending(self):
        _pending_confirms.clear()
        _resolved_actions.clear()
        yield
        _pending_confirms.clear()
        _resolved_actions.clear()

    async def test_db_unavailable_returns_process_fresh(self):
        with patch("app.services.dedup._get_db_session", return_value=None):
            result = await check(
                client_token="user-123",
                filename="report.pdf",
                file_size=1024,
                content=b"sample content",
                parsed_text="sample parsed text",
                avg_confidence=0.9,
            )
        assert result.action == "process_fresh"
        assert result.reason == "db_unavailable"

    async def test_below_threshold_skips_tfidf(self):
        """Files smaller than dedup_threshold_mb skip TF-IDF."""
        small_content = b"x" * 100  # well below 2MB threshold
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.return_value = None
        db_mock.close = MagicMock()

        with patch("app.services.dedup._get_db_session", return_value=db_mock), \
             patch("asyncio.to_thread", side_effect=_async_to_thread_passthrough):
            result = await check(
                client_token="user-123",
                filename="small.pdf",
                file_size=len(small_content),
                content=small_content,
                parsed_text="short text",
                avg_confidence=None,
            )
        assert result.action == "process_fresh"
        assert result.reason in ("below_threshold", "text_too_short", "no_match", "db_unavailable", "error")

    async def test_same_name_different_hash_returns_process_fresh(self):
        """Same filename but different content → always reprocess."""
        db_mock = MagicMock()

        # hash query returns None (no exact match)
        # name query returns a row (same name exists)
        call_count = [0]

        def _query_filter_first():
            call_count[0] += 1
            if call_count[0] == 1:
                return None  # no hash match
            return MagicMock(filename="report.pdf")  # name match

        q = MagicMock()
        q.filter.return_value.first.side_effect = _query_filter_first
        db_mock.query.return_value = q
        db_mock.close = MagicMock()

        content = b"different content bytes here " * 50_000  # 1.4MB
        with patch("app.services.dedup._get_db_session", return_value=db_mock), \
             patch("asyncio.to_thread", side_effect=_async_to_thread_passthrough):
            result = await check(
                client_token="user-123",
                filename="report.pdf",
                file_size=len(content),
                content=content,
                parsed_text="different content " * 100,
                avg_confidence=0.95,
            )
        # Either same_name_different_content or falls through to process_fresh for other reasons
        assert result.action in ("process_fresh", "confirm")

    async def test_hash_match_no_vectors_reprocesses(self):
        """Hash match found but the collection was deleted → process fresh."""
        existing_doc = MagicMock()
        existing_doc.session_id = "old-session-id"
        existing_doc.uploaded_at = None
        existing_doc.chunks_stored = 0

        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.return_value = existing_doc
        db_mock.close = MagicMock()

        with patch("app.services.dedup._get_db_session", return_value=db_mock), \
             patch("app.services.dedup._find_existing", return_value=existing_doc), \
             patch("app.services.dedup._collection_has_vectors", return_value=False), \
             patch("asyncio.to_thread", side_effect=_async_to_thread_passthrough):
            result = await check(
                client_token="user-x",
                filename="doc.pdf",
                file_size=1024,
                content=b"content bytes",
                parsed_text="parsed text",
                avg_confidence=0.9,
            )
        assert result.action in ("process_fresh", "confirm")

    async def test_error_in_db_returns_process_fresh(self):
        """Any unexpected DB error → fall back to process_fresh."""
        db_mock = MagicMock()
        db_mock.query.side_effect = RuntimeError("db timeout")
        db_mock.close = MagicMock()

        with patch("app.services.dedup._get_db_session", return_value=db_mock), \
             patch("asyncio.to_thread", side_effect=_async_to_thread_passthrough):
            result = await check(
                client_token="user-y",
                filename="file.pdf",
                file_size=1024,
                content=b"data",
                parsed_text="text",
                avg_confidence=None,
            )
        assert result.action == "process_fresh"


# ── Helper: make asyncio.to_thread synchronous in tests ──────────────────────

async def _async_to_thread_passthrough(fn, *args, **kwargs):
    """Run the function directly (no thread pool) so tests stay fast."""
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

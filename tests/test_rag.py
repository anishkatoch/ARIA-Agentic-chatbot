"""
Tests for app/services/rag.py pure helpers.
No LLM calls, no vector store — only chunk_text*, find_page.
"""
import pytest
from app.services.rag import chunk_text, chunk_text_with_offsets, find_page


# ── chunk_text ────────────────────────────────────────────────────────────────

class TestChunkText:
    def test_short_text_one_chunk(self):
        chunks = chunk_text("short text")
        assert len(chunks) == 1
        assert chunks[0] == "short text"

    def test_long_text_multiple_chunks(self):
        text = "word " * 600  # ~3000 chars → at least 3 chunks of 1000
        chunks = chunk_text(text)
        assert len(chunks) >= 3

    def test_each_chunk_at_most_1000_chars(self):
        text = "x " * 2000
        chunks = chunk_text(text)
        for chunk in chunks:
            assert len(chunk) <= 1000

    def test_overlap_content_appears_in_adjacent_chunks(self):
        text = "a " * 1000
        chunks = chunk_text(text)
        if len(chunks) > 1:
            # Due to 200-char overlap, end of chunk N overlaps with start of chunk N+1
            end_of_first  = chunks[0][-100:]
            start_of_second = chunks[1][:100]
            # Some content should be shared
            assert any(w in chunks[1] for w in chunks[0].split()[-10:])

    def test_empty_text_returns_empty_list(self):
        chunks = chunk_text("")
        assert chunks == []


# ── chunk_text_with_offsets ───────────────────────────────────────────────────

class TestChunkTextWithOffsets:
    def test_returns_tuples(self):
        result = chunk_text_with_offsets("hello world test content here")
        assert isinstance(result, list)
        assert all(isinstance(r, tuple) and len(r) == 2 for r in result)

    def test_first_offset_is_zero(self):
        result = chunk_text_with_offsets("hello world " * 100)
        assert result[0][1] == 0

    def test_offsets_are_non_decreasing(self):
        text = "word " * 500
        result = chunk_text_with_offsets(text)
        offsets = [r[1] for r in result]
        assert offsets == sorted(offsets)

    def test_offsets_are_integers(self):
        result = chunk_text_with_offsets("text " * 300)
        for _, offset in result:
            assert isinstance(offset, int)

    def test_chunk_text_at_offset_matches_source(self):
        text = "abcdef " * 500
        result = chunk_text_with_offsets(text)
        for chunk, offset in result:
            # The chunk should start where the offset says it does
            assert text[offset:offset + len(chunk[:20])] == chunk[:20]

    def test_long_text_more_chunks_than_short(self):
        short = chunk_text_with_offsets("hello")
        long  = chunk_text_with_offsets("word " * 1000)
        assert len(long) > len(short)


# ── find_page ─────────────────────────────────────────────────────────────────

class TestFindPage:
    def setup_method(self):
        self.spans = [
            {"page_number": 1, "start": 0,   "end": 100,  "confidence": 0.95},
            {"page_number": 2, "start": 100, "end": 200,  "confidence": 0.88},
            {"page_number": 3, "start": 200, "end": 350,  "confidence": None},
        ]

    def test_offset_in_first_span(self):
        page, conf = find_page(0, self.spans)
        assert page == 1
        assert conf == 0.95

    def test_offset_at_span_boundary(self):
        page, conf = find_page(100, self.spans)
        assert page == 2

    def test_offset_in_middle_span(self):
        page, conf = find_page(150, self.spans)
        assert page == 2
        assert conf == 0.88

    def test_offset_in_last_span(self):
        page, conf = find_page(300, self.spans)
        assert page == 3
        assert conf is None

    def test_offset_beyond_all_spans_falls_back_to_last(self):
        page, conf = find_page(9999, self.spans)
        assert page == 3  # last span fallback

    def test_single_span_always_returns_it(self):
        spans = [{"page_number": 1, "start": 0, "end": 500, "confidence": 0.99}]
        page, conf = find_page(0, spans)
        assert page == 1
        page, conf = find_page(9999, spans)  # beyond end → fallback
        assert page == 1

    def test_confidence_none_propagated(self):
        _, conf = find_page(300, self.spans)
        assert conf is None

    def test_confidence_value_propagated(self):
        _, conf = find_page(50, self.spans)
        assert conf == 0.95

    def test_txt_file_single_span(self):
        """parse_file returns a single span for .txt files."""
        text = "hello world this is a test document"
        spans = [{"page_number": 1, "start": 0, "end": len(text), "confidence": None}]
        page, conf = find_page(5, spans)
        assert page == 1
        assert conf is None

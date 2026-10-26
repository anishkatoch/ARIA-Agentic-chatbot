"""Tests for app.services.rag._dedupe_to_latest_version - the retrieval-time
guard against mixing two versions of the same document into one answer when
they share a filename (see planning/PLAN-document-versioning.md).

Critical correctness requirement: must be a complete no-op for chunks that
predate version metadata (every KB uploaded before this feature existed) -
a bug here would silently empty out retrieval for all pre-existing content.
"""
from app.services.rag import _dedupe_to_latest_version


def _item(text, **meta):
    return (text, meta)


def _get_meta(item):
    return item[1]


class TestNoVersionMetadata:
    def test_untagged_chunks_pass_through_unchanged(self):
        """The critical regression guard: chunks with no version/item_id
        metadata at all (every pre-existing upload) must never be dropped."""
        items = [
            _item("chunk a", source="report.pdf", chunk_index=0),
            _item("chunk b", source="report.pdf", chunk_index=1),
            _item("chunk c", source="other.pdf", chunk_index=0),
        ]
        result = _dedupe_to_latest_version(items, get_meta=_get_meta)
        assert result == items

    def test_empty_list(self):
        assert _dedupe_to_latest_version([], get_meta=_get_meta) == []

    def test_mixed_tagged_and_untagged_keeps_untagged_untouched(self):
        tagged_old = _item("old", source="doc.pdf", item_id="x", version=1)
        tagged_new = _item("new", source="doc.pdf", item_id="x", version=2)
        untagged = _item("unrelated", source="legacy.pdf")
        result = _dedupe_to_latest_version([tagged_old, tagged_new, untagged], get_meta=_get_meta)
        assert untagged in result
        assert tagged_new in result
        assert tagged_old not in result


class TestVersionDedup:
    def test_keeps_only_highest_version_for_shared_item_id(self):
        v1 = _item("old content", source="document.pdf", item_id="1101", version=1)
        v2 = _item("new content", source="document_v2.pdf", item_id="1101", version=2)
        result = _dedupe_to_latest_version([v1, v2], get_meta=_get_meta)
        assert result == [v2]

    def test_falls_back_to_source_filename_when_no_item_id(self):
        """Same filename reused across versions without an item_id - still
        deduped by filename as the identity key."""
        v1 = _item("old content", source="document.pdf", version=1)
        v2 = _item("new content", source="document.pdf", version=2)
        result = _dedupe_to_latest_version([v1, v2], get_meta=_get_meta)
        assert result == [v2]

    def test_three_versions_keeps_only_latest(self):
        v1 = _item("v1", source="doc.pdf", item_id="x", version=1)
        v2 = _item("v2", source="doc.pdf", item_id="x", version=2)
        v3 = _item("v3", source="doc.pdf", item_id="x", version=3)
        result = _dedupe_to_latest_version([v3, v1, v2], get_meta=_get_meta)
        assert result == [v3]

    def test_different_documents_each_keep_their_own_latest(self):
        a1 = _item("a1", source="a.pdf", item_id="a", version=1)
        a2 = _item("a2", source="a.pdf", item_id="a", version=2)
        b1 = _item("b1", source="b.pdf", item_id="b", version=1)
        result = _dedupe_to_latest_version([a1, a2, b1], get_meta=_get_meta)
        assert set(r[0] for r in result) == {"a2", "b1"}

    def test_single_version_document_keeps_its_only_chunk(self):
        v1 = _item("only version", source="solo.pdf", item_id="s", version=1)
        result = _dedupe_to_latest_version([v1], get_meta=_get_meta)
        assert result == [v1]

    def test_works_with_document_like_objects_not_just_tuples(self):
        class FakeDoc:
            def __init__(self, page_content, metadata):
                self.page_content = page_content
                self.metadata = metadata

        old = FakeDoc("old", {"source": "d.pdf", "item_id": "x", "version": 1})
        new = FakeDoc("new", {"source": "d.pdf", "item_id": "x", "version": 2})
        result = _dedupe_to_latest_version([old, new], get_meta=lambda d: d.metadata)
        assert result == [new]

"""Text diff for comparing two document versions (or any two documents).

Deliberately a plain text diff over previously-extracted document text, not
an embedding/vector comparison - embedding similarity tells you how alike
two things are, not what specifically changed. See
planning/PLAN-document-versioning.md "Diff feature".
"""
import difflib
import re
from collections import Counter


def normalize_text(text: str) -> list[str]:
    """Collapses repeated whitespace and strips lines that repeat verbatim
    across the document (headers/footers/page numbers from PDF/DOCX
    extraction) before diffing, so re-wrapped lines or page breaks don't show
    up as fake changes. Returns a list of normalized, non-empty lines."""
    if not text:
        return []

    raw_lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    raw_lines = [line for line in raw_lines if line]

    if len(raw_lines) > 10:
        counts = Counter(raw_lines)
        # A line repeating on at least ~20% of the document's lines (and at
        # least 3 times) is treated as a running header/footer/page-number
        # artifact, not real content - short lines only, to avoid stripping
        # a genuinely repeated short sentence in normal prose.
        repeat_threshold = max(3, len(raw_lines) // 5)
        noisy = {
            line for line, count in counts.items()
            if count >= repeat_threshold and len(line) <= 80
        }
        raw_lines = [line for line in raw_lines if line not in noisy]

    return raw_lines


def diff_texts(old_text: str, new_text: str) -> dict:
    """Unified diff between two documents' extracted text, after
    normalization. Returns {"lines": [{"type", "text"}, ...],
    "lines_added": int, "lines_removed": int}."""
    old_lines = normalize_text(old_text)
    new_lines = normalize_text(new_text)

    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    result_lines: list[dict] = []
    added = removed = 0

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for line in old_lines[i1:i2]:
                result_lines.append({"type": "unchanged", "text": line})
        elif tag == "delete":
            for line in old_lines[i1:i2]:
                result_lines.append({"type": "removed", "text": line})
                removed += 1
        elif tag == "insert":
            for line in new_lines[j1:j2]:
                result_lines.append({"type": "added", "text": line})
                added += 1
        elif tag == "replace":
            for line in old_lines[i1:i2]:
                result_lines.append({"type": "removed", "text": line})
                removed += 1
            for line in new_lines[j1:j2]:
                result_lines.append({"type": "added", "text": line})
                added += 1

    return {"lines": result_lines, "lines_added": added, "lines_removed": removed}

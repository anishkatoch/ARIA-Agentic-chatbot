from app.services.diff import diff_texts, normalize_text


def test_normalize_collapses_whitespace():
    text = "Hello   world  \n\n  Foo\tbar"
    lines = normalize_text(text)
    assert lines == ["Hello world", "Foo bar"]


def test_normalize_strips_repeated_header_footer():
    # A short line repeating on most pages (header/footer/page-number noise)
    body = ["Page 1"] + [f"Content line {i}" for i in range(20)] + ["Page 1"] * 4
    text = "\n".join(body)
    lines = normalize_text(text)
    assert "Page 1" not in lines
    assert "Content line 0" in lines


def test_diff_detects_simple_change():
    old = "Enterprise tier: $499/mo\nStarter tier: $19/mo"
    new = "Enterprise tier: $599/mo\nStarter tier: $19/mo"
    result = diff_texts(old, new)
    assert result["lines_removed"] == 1
    assert result["lines_added"] == 1
    removed = [l["text"] for l in result["lines"] if l["type"] == "removed"]
    added = [l["text"] for l in result["lines"] if l["type"] == "added"]
    assert "Enterprise tier: $499/mo" in removed
    assert "Enterprise tier: $599/mo" in added
    unchanged = [l["text"] for l in result["lines"] if l["type"] == "unchanged"]
    assert "Starter tier: $19/mo" in unchanged


def test_diff_identical_text_has_no_changes():
    text = "line one\nline two\nline three"
    result = diff_texts(text, text)
    assert result["lines_added"] == 0
    assert result["lines_removed"] == 0
    assert all(l["type"] == "unchanged" for l in result["lines"])


def test_diff_detects_added_section():
    old = "Intro\nEligibility: 18 years"
    new = "Intro\nEligibility: 18 years\nRemote applications are now accepted."
    result = diff_texts(old, new)
    assert result["lines_added"] == 1
    assert result["lines_removed"] == 0
    added = [l["text"] for l in result["lines"] if l["type"] == "added"]
    assert "Remote applications are now accepted." in added


def test_diff_empty_old_text_is_all_additions():
    result = diff_texts("", "brand new content\nsecond line")
    assert result["lines_removed"] == 0
    assert result["lines_added"] == 2

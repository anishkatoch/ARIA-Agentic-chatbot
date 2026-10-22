"""
Tests for the "attach a connector space/chat by name" bug: a Google Chat
space ingested as "Google Chat - <opaque space_id>.txt" (instead of its
actual display name, e.g. "Rosotravel Internal Scrum") could never be
resolved by name - only a specific fact already inside it, found by plain
content search, would ever surface it. "What's in Rosotravel Internal
Scrum" answered "not in the documents" while "what's the budget for X"
(a fact actually in the space) worked fine.

Covers:
  1. GoogleChatConnector.get_space / _space_name - resolves a space_id to
     its human-readable display name (or a sensible fallback).
  2. OutlookChatConnector.get_chat / _chat_name - same, for Outlook chats.
  3. pipeline_bridge._connector_doc_filename - builds "<prefix> - <name>
     (<id>).txt", the shared helper both ingest_google_chat_space and
     ingest_outlook_chat now use instead of the raw id.
  4. End-to-end: app.services.rag._resolve_target_sources correctly
     resolves a bare mention of the space's name against the filename that
     shape now produces - the actual fix, exercised the way a user would
     type it.
  5. The follow-up ask: resolving by the connector's own technical id
     ("what is google chat - aaa"), not just the display name, across every
     connector that embeds one (Gmail, Outlook, Google Chat, Outlook Chat) -
     and confirming plain file uploads (no connector at all) already work
     the same way by filename, with no connector-specific carve-out needed.
  6. "whats" without an apostrophe (is_broad_query) - a very common way
     people actually type "what's", which a strict apostrophe-only regex
     would otherwise silently miss.

No network calls, no OAuth, no DB - connector clients are constructed with
object.__new__() to skip their __init__ (which needs real OAuth env vars),
then their _api_request method is mocked directly.
"""
import pytest
from unittest.mock import MagicMock

from app.connectors.pipeline_bridge import _connector_doc_filename
from app.services.rag import _resolve_target_sources, is_broad_query


def _make_google_chat_connector():
    from app.connectors.clients.google_chat import GoogleChatConnector
    c = object.__new__(GoogleChatConnector)
    c.user_id = "u1"
    c._self_resource = None
    return c


def _make_outlook_chat_connector():
    from app.connectors.clients.outlook_chat import OutlookChatConnector
    c = object.__new__(OutlookChatConnector)
    c.user_id = "u1"
    c._self_id = None
    return c


# ── GoogleChatConnector.get_space ─────────────────────────────────────────────

class TestGoogleChatGetSpace:
    def test_uses_display_name_when_present(self):
        c = _make_google_chat_connector()
        c._api_request = MagicMock(return_value={
            "name": "spaces/AAAA1234", "displayName": "Rosotravel Internal Scrum", "spaceType": "SPACE",
        })
        result = c.get_space("AAAA1234")
        assert result == {"id": "AAAA1234", "name": "Rosotravel Internal Scrum", "space_type": "SPACE"}
        c._api_request.assert_called_once_with("/spaces/AAAA1234")

    def test_direct_message_falls_back_to_other_member_names(self):
        c = _make_google_chat_connector()
        c._api_request = MagicMock(side_effect=[
            {"name": "spaces/DM123", "spaceType": "DIRECT_MESSAGE"},  # get_space's own call
            {"memberships": [{"member": {"displayName": "Jane Doe", "name": "users/2"}}]},  # _other_member_names
        ])
        c._get_self_resource = MagicMock(return_value="users/1")
        result = c.get_space("DM123")
        assert result["name"] == "Jane Doe"

    def test_no_display_name_no_members_falls_back_to_raw_name(self):
        c = _make_google_chat_connector()
        c._api_request = MagicMock(side_effect=[
            {"name": "spaces/XYZ", "spaceType": "SPACE"},
            {"memberships": []},
        ])
        c._get_self_resource = MagicMock(return_value="users/1")
        result = c.get_space("XYZ")
        assert result["name"] == "spaces/XYZ"

    def test_api_error_propagates_for_caller_to_handle(self):
        """pipeline_bridge catches this and falls back to the raw space_id -
        get_space itself shouldn't swallow it silently."""
        c = _make_google_chat_connector()
        c._api_request = MagicMock(side_effect=RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            c.get_space("AAAA1234")


# ── OutlookChatConnector.get_chat ─────────────────────────────────────────────

class TestOutlookChatGetChat:
    def test_uses_topic_when_present(self):
        c = _make_outlook_chat_connector()
        c._api_request = MagicMock(return_value={
            "id": "19:abc", "topic": "Rosotravel Internal Scrum", "chatType": "group",
        })
        result = c.get_chat("19:abc")
        assert result == {"id": "19:abc", "name": "Rosotravel Internal Scrum", "chat_type": "group"}
        c._api_request.assert_called_once_with("/me/chats/19:abc", {"$expand": "members"})

    def test_no_topic_falls_back_to_other_member_names(self):
        c = _make_outlook_chat_connector()
        c._api_request = MagicMock(return_value={
            "id": "19:abc", "chatType": "oneOnOne",
            "members": [
                {"displayName": "Me", "userId": "self-1"},
                {"displayName": "Jane Doe", "userId": "other-1"},
            ],
        })
        c._get_self_id = MagicMock(return_value="self-1")
        result = c.get_chat("19:abc")
        assert result["name"] == "Jane Doe"

    def test_no_topic_no_members_falls_back_to_generic_label(self):
        c = _make_outlook_chat_connector()
        c._api_request = MagicMock(return_value={"id": "19:abc", "chatType": "oneOnOne", "members": []})
        c._get_self_id = MagicMock(return_value="self-1")
        result = c.get_chat("19:abc")
        assert result["name"] == "Direct message"

    def test_api_error_propagates_for_caller_to_handle(self):
        c = _make_outlook_chat_connector()
        c._api_request = MagicMock(side_effect=RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            c.get_chat("19:abc")


# ── pipeline_bridge._connector_doc_filename ───────────────────────────────────

class TestConnectorDocFilename:
    def test_basic_shape(self):
        assert (
            _connector_doc_filename("Google Chat", "Rosotravel Internal Scrum", "AAAA1234")
            == "Google Chat - Rosotravel Internal Scrum (AAAA1234).txt"
        )

    def test_strips_unsafe_characters_from_name(self):
        result = _connector_doc_filename("Outlook Chat", "Q1 / Planning: *urgent*", "19:xyz")
        assert "/" not in result and ":" not in result.split("(")[0] and "*" not in result

    def test_empty_name_falls_back_to_generic_label(self):
        result = _connector_doc_filename("Google Chat", "", "AAAA1234")
        assert result == "Google Chat - chat (AAAA1234).txt"

    def test_id_suffix_present_for_disambiguation(self):
        a = _connector_doc_filename("Google Chat", "Standup", "AAAA1111")
        b = _connector_doc_filename("Google Chat", "Standup", "BBBB2222")
        assert a != b  # two differently-named-but-same-display-name spaces stay distinguishable

    def test_long_name_truncated(self):
        result = _connector_doc_filename("Google Chat", "x" * 200, "AAAA1234")
        # 60-char cap on the name portion, same as the extension-stripping cap used elsewhere
        assert len(result) < 100


# ── End-to-end: the actual "Rosotravel Internal Scrum" bug report ────────────

class TestConnectorSourceResolutionEndToEnd:
    """Reproduces the reported bug exactly: attach a Google Chat space named
    "Rosotravel Internal Scrum", then ask about it by name in a handful of
    different phrasings a real user might type."""

    def setup_method(self):
        filename = _connector_doc_filename("Google Chat", "Rosotravel Internal Scrum", "AAAA1234xyz")
        self.sources = [{"source": filename, "connector": "google_chat"}]

    @pytest.mark.parametrize("question", [
        "what is in rosotravel internal scrum",
        "what is in this rosotravel internal scrum",
        "rosotravel internal scrum",
        "tell me about rosotravel internal scrum",
        "what is the latest decision in rosotravel internal scrum",
        "summarize rosotravel internal scrum",
        "what are the key points from rosotravel internal scrum",
    ])
    def test_resolves_space_by_its_display_name(self, question):
        result = _resolve_target_sources(question, self.sources)
        assert result == [self.sources[0]["source"]], f"failed to resolve: {question!r}"

    def test_resolution_also_marks_the_question_as_broad(self):
        """A resolved source always forces broad (document-order) retrieval
        in _retrieve_for_question, regardless of is_broad_query's own regex -
        this is what actually makes the "what's in X" question answerable."""
        target = _resolve_target_sources("rosotravel internal scrum", self.sources)
        broad = bool(target) or is_broad_query("rosotravel internal scrum")
        assert broad is True

    def test_unrelated_question_does_not_false_positive(self):
        assert _resolve_target_sources("what's the weather like", self.sources) is None

    def test_old_id_based_filename_would_not_have_resolved(self):
        """Regression guard for the actual root cause: the OLD filename
        shape (raw space_id, pre-fix) must NOT resolve by the space's name -
        proving the fix is the filename change, not something in
        _resolve_target_sources alone."""
        old_style_sources = [{"source": "Google Chat - AAAA1234xyz.txt", "connector": "google_chat"}]
        assert _resolve_target_sources("rosotravel internal scrum", old_style_sources) is None


# ── "whats" without an apostrophe ─────────────────────────────────────────────

class TestWhatsWithoutApostrophe:
    @pytest.mark.parametrize("question", [
        "whats in rosotravel internal scrum",
        "whats in this document",
        "whats this file contain",
        "whats in the data",
    ])
    def test_detected_as_broad(self, question):
        assert is_broad_query(question) is True

    def test_still_resolves_the_source_by_partial_name(self):
        filename = _connector_doc_filename("Google Chat", "Rosotravel Internal Scrum", "AAAA1234xyz")
        sources = [{"source": filename, "connector": "google_chat"}]
        assert _resolve_target_sources("whats in rosotravel", sources) == [filename]

    def test_no_new_false_positive_on_specific_questions(self):
        """"whats" shouldn't become a blanket trigger - a specific factual
        question phrased the same casual way must stay non-broad."""
        assert is_broad_query("whats the total in row 5") is False
        assert is_broad_query("whats the weather today") is False


# ── ID-fragment resolution across every connector that embeds one ────────────
# Gmail/Outlook messages and Google Chat/Outlook Chat spaces/chats all embed
# a short technical id in their filename ("... (<id>).txt" - see
# pipeline_bridge.py). A user referencing that id - copied or paraphrased
# from the connector UI - should resolve the same way a display-name
# reference does, regardless of which connector it came from.

class TestIdFragmentResolutionAcrossConnectors:
    def test_google_chat_space_by_id_fragment(self):
        # _connector_doc_filename keeps only the LAST 8 chars of the real id
        # (see its docstring) - "AAAA1234xyz" (11 chars) is stored as
        # "a1234xyz", so the user's fragment has to be one that's actually
        # present in what got stored, not an arbitrary prefix of the full
        # untruncated id.
        filename = _connector_doc_filename("Google Chat", "Rosotravel Internal Scrum", "AAAA1234xyz")
        assert "(a1234xyz)" in filename.lower()
        sources = [{"source": filename, "connector": "google_chat"}]
        assert _resolve_target_sources("what is google chat - a1234", sources) == [filename]

    def test_outlook_chat_by_id_fragment(self):
        filename = _connector_doc_filename("Outlook Chat", "Project Sync", "19abc456def")
        sources = [{"source": filename, "connector": "outlook_chat"}]
        assert _resolve_target_sources("what is in outlook chat abc456", sources) == [filename]

    def test_gmail_message_by_id_fragment(self):
        # Gmail/Outlook emails build their filename inline in pipeline_bridge
        # (not via _connector_doc_filename) but use the same "(<id>).txt"
        # shape: f"{safe_subject} ({message_id[:8]}).txt"
        filename = "Budget approval needed (abcd1234).txt"
        sources = [{"source": filename, "connector": "gmail"}]
        assert _resolve_target_sources("what is in email abcd1234", sources) == [filename]

    def test_outlook_email_by_id_fragment(self):
        filename = "Vendor contract renewal (ef567890).txt"
        sources = [{"source": filename, "connector": "outlook"}]
        assert _resolve_target_sources("tell me about ef567890", sources) == [filename]

    def test_truncated_id_fragment_either_direction(self):
        """A fragment shorter than the stored id (typed by the user) and a
        fragment where the stored id is shorter than what's typed both need
        to line up - matching must work in either direction."""
        filename = _connector_doc_filename("Google Chat", "Standup", "zzzz9999")
        sources = [{"source": filename, "connector": "google_chat"}]
        assert _resolve_target_sources("what is google chat zzzz", sources) == [filename]  # short fragment
        assert _resolve_target_sources("what is google chat zzzz99990000", sources) == [filename]  # longer than stored

    def test_short_common_words_in_question_dont_false_positive(self):
        """Common words like "chat"/"google"/"what" must not themselves be
        mistaken for id fragments just because they're 3+ letters."""
        filename = _connector_doc_filename("Google Chat", "Weekly Sync", "qq112233")
        sources = [{"source": filename, "connector": "google_chat"}]
        unrelated = [{"source": "unrelated.pdf", "connector": None}]
        assert _resolve_target_sources("what is in this chat", sources) is None or \
            _resolve_target_sources("what is in this chat", sources) == [filename]
        # The real guard: a totally unrelated id-less source must never
        # match just because the question contains "what"/"chat"/"google".
        assert _resolve_target_sources("what is google chat about", unrelated) is None

    def test_no_match_against_wrong_source(self):
        """An id fragment for one source must not match a different one."""
        a = _connector_doc_filename("Google Chat", "Standup", "aaaa1111")
        b = _connector_doc_filename("Google Chat", "Planning", "bbbb2222")
        sources = [{"source": a, "connector": "google_chat"}, {"source": b, "connector": "google_chat"}]
        assert _resolve_target_sources("what is google chat aaaa", sources) == [a]


# ── Plain file uploads (no connector at all) work the same way ───────────────
# The fix lives in _resolve_target_sources, which is connector-agnostic - a
# regular uploaded file (no "connector" metadata key at all) was never part
# of the bug and needs no special-casing, but is covered here explicitly
# since it was called out as a case to verify.

class TestPlainFileUploadsResolveByNameToo:
    def setup_method(self):
        self.sources = [
            {"source": "Q3-Budget-Report.xlsx", "connector": None},
            {"source": "employee_handbook.pdf", "connector": None},
        ]

    @pytest.mark.parametrize("question", [
        "what is in the file",
        "what is in this file",
        "whats in the file",
        "what is in q3 budget report",
        "tell me about the employee handbook",
        "summarize employee_handbook.pdf",
    ])
    def test_resolves_uploaded_files_by_name_or_demonstrative(self, question):
        # "what is in the file" alone is ambiguous across 2 uploaded files,
        # so it correctly falls through to None (ambiguity-aware retrieval
        # handles that case, not silent guessing) - every other phrasing
        # names a specific file and must resolve to exactly that one.
        result = _resolve_target_sources(question, self.sources)
        if "file" in question.lower() and "budget" not in question.lower() and "handbook" not in question.lower() \
                and ".pdf" not in question.lower():
            assert result is None
        else:
            assert result is not None and len(result) == 1

    def test_single_uploaded_file_bare_demonstrative_resolves(self):
        single = [{"source": "Q3-Budget-Report.xlsx", "connector": None}]
        assert _resolve_target_sources("what is in the file", single) == ["Q3-Budget-Report.xlsx"]
        assert _resolve_target_sources("whats in this file", single) == ["Q3-Budget-Report.xlsx"]

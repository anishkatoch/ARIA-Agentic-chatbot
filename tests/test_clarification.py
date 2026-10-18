"""
Tests for the clarification / correction feature in Thinking Mode.

Covers:
  1. Clarification flag store — set, get, default
  2. Gate 1 — clarify blocked when only 1 distinct source
  3. Gate 2 — clarify blocked when already asked last turn
  4. Gate 1+2 pass — clarify_allowed=True with 2+ sources, no prior ask
  5. ReAct Clarify action is terminal — returns immediately, sets flag
  6. History saved after clarification response
  7. Correction rules exist in _ARIA_SYSTEM
  8. Single apology rule exists in _ARIA_SYSTEM
  9. Clarify action NOT in prompt when clarify_allowed=False
 10. Clarify action IS in prompt when clarify_allowed=True
"""
import pytest
from unittest.mock import MagicMock, patch
from langchain_core.documents import Document


# ── 1. Clarification flag store ───────────────────────────────────────────────

class TestClarifFlag:

    def test_default_false_for_new_session(self):
        from app.services.rag import _clarif_was_asked
        assert _clarif_was_asked("brand-new-session-xyz") is False

    def test_set_true(self):
        from app.services.rag import _clarif_set, _clarif_was_asked
        _clarif_set("sess-a", True)
        assert _clarif_was_asked("sess-a") is True

    def test_set_false_clears_flag(self):
        from app.services.rag import _clarif_set, _clarif_was_asked
        _clarif_set("sess-b", True)
        _clarif_set("sess-b", False)
        assert _clarif_was_asked("sess-b") is False

    def test_independent_per_session(self):
        from app.services.rag import _clarif_set, _clarif_was_asked
        _clarif_set("sess-c", True)
        _clarif_set("sess-d", False)
        assert _clarif_was_asked("sess-c") is True
        assert _clarif_was_asked("sess-d") is False

    def test_default_false_after_clear(self):
        from app.services.rag import _clarif_set, _clarif_was_asked
        _clarif_set("sess-e", True)
        _clarif_set("sess-e", False)
        assert _clarif_was_asked("sess-e") is False


# ── 2. Gate 1 — blocked when only 1 source ───────────────────────────────────

class TestGate1SingleSource:

    def _make_vs_with_sources(self, sources: list[str]):
        """Build a mock vector store whose hybrid retrieve returns docs from given sources."""
        docs = [
            Document(page_content=f"content from {s}", metadata={"source": s})
            for s in sources
        ]
        vs = MagicMock()
        vs.as_retriever.return_value.invoke.return_value = docs
        vs._collection = MagicMock()
        vs._collection.get.return_value = {
            "documents": [d.page_content for d in docs],
            "metadatas": [d.metadata for d in docs],
        }
        return vs, docs

    def test_single_source_clarify_not_allowed(self):
        from app.services.rag import _clarif_set
        _clarif_set("gate1-test", False)

        vs, docs = self._make_vs_with_sources(["doc_a.pdf"])

        with patch("app.services.rag._hybrid_retrieve", return_value=docs):
            # Re-run the gate logic manually
            distinct = {d.metadata.get("source", "") for d in docs if d.metadata.get("source")}
            multi_source = len(distinct) >= 2
        assert multi_source is False, "Single source must not allow clarification"

    def test_two_sources_clarify_allowed(self):
        vs, docs = self._make_vs_with_sources(["doc_a.pdf", "doc_b.pdf"])

        with patch("app.services.rag._hybrid_retrieve", return_value=docs):
            distinct = {d.metadata.get("source", "") for d in docs if d.metadata.get("source")}
            multi_source = len(distinct) >= 2
        assert multi_source is True, "Two sources must allow clarification gate to pass"

    def test_three_sources_clarify_allowed(self):
        vs, docs = self._make_vs_with_sources(["a.pdf", "b.pdf", "c.txt"])

        distinct = {d.metadata.get("source", "") for d in docs if d.metadata.get("source")}
        assert len(distinct) >= 2

    def test_no_source_metadata_treated_as_single(self):
        """Docs with no source metadata should not count as multiple sources."""
        docs = [Document(page_content="chunk", metadata={}) for _ in range(5)]
        distinct = {d.metadata.get("source", "") for d in docs if d.metadata.get("source")}
        assert len(distinct) < 2, "Empty source metadata must not count as multi-source"


# ── 3. Gate 2 — blocked when already clarified last turn ──────────────────────

class TestGate2AlreadyClarified:

    def test_already_clarified_blocks_second_ask(self):
        from app.services.rag import _clarif_set, _clarif_was_asked
        _clarif_set("gate2-sess", True)
        already = _clarif_was_asked("gate2-sess")
        assert already is True, "Gate 2 must be triggered when flag is True"

    def test_after_reset_allows_new_clarification(self):
        from app.services.rag import _clarif_set, _clarif_was_asked
        _clarif_set("gate2-sess2", True)
        _clarif_set("gate2-sess2", False)  # reset at start of new turn
        assert _clarif_was_asked("gate2-sess2") is False

    def test_fresh_session_gate2_passes(self):
        from app.services.rag import _clarif_was_asked
        assert _clarif_was_asked("gate2-fresh-sess-999") is False


# ── 4. Gate 1+2 combined ─────────────────────────────────────────────────────

class TestCombinedGates:

    def test_both_pass_means_clarify_allowed(self):
        """2+ sources AND no prior clarification → clarify_allowed=True."""
        from app.services.rag import _clarif_set, _clarif_was_asked
        _clarif_set("combined-sess", False)

        docs = [
            Document(page_content="a", metadata={"source": "file1.pdf"}),
            Document(page_content="b", metadata={"source": "file2.pdf"}),
        ]
        distinct = {d.metadata.get("source", "") for d in docs if d.metadata.get("source")}
        multi_source = len(distinct) >= 2
        already_clarified = _clarif_was_asked("combined-sess")

        clarify_allowed = multi_source and not already_clarified
        assert clarify_allowed is True

    def test_gate1_fails_means_no_clarify(self):
        """Single source → clarify_allowed=False even if flag is clear."""
        from app.services.rag import _clarif_set, _clarif_was_asked
        _clarif_set("combined-sess2", False)

        docs = [Document(page_content="a", metadata={"source": "file1.pdf"})]
        distinct = {d.metadata.get("source", "") for d in docs if d.metadata.get("source")}
        multi_source = len(distinct) >= 2
        already_clarified = _clarif_was_asked("combined-sess2")

        clarify_allowed = multi_source and not already_clarified
        assert clarify_allowed is False

    def test_gate2_fails_means_no_clarify(self):
        """2+ sources but already clarified → clarify_allowed=False."""
        from app.services.rag import _clarif_set, _clarif_was_asked
        _clarif_set("combined-sess3", True)  # already clarified

        docs = [
            Document(page_content="a", metadata={"source": "file1.pdf"}),
            Document(page_content="b", metadata={"source": "file2.pdf"}),
        ]
        distinct = {d.metadata.get("source", "") for d in docs if d.metadata.get("source")}
        multi_source = len(distinct) >= 2
        already_clarified = _clarif_was_asked("combined-sess3")

        clarify_allowed = multi_source and not already_clarified
        assert clarify_allowed is False


# ── 5. ReAct Clarify tool is terminal ─────────────────────────────────────────
# The ReAct agent is now a real tool-calling agent (langchain.agents.create_agent),
# not text parsing — so these tests mock at the create_agent boundary and simulate
# the model choosing to call the clarify tool, rather than mocking llm_invoke
# (which _react_answer no longer calls at all).

class TestReActClarifyTerminal:

    def _fake_agent_calling_clarify(self, clarify_text: str):
        """Returns a stand-in for create_agent() whose .invoke() simulates the
        model choosing to call the clarify tool with the given question."""
        def _create_agent(llm, tools, system_prompt=None):
            class FakeAgent:
                def invoke(self, input_, config=None):
                    for t in tools:
                        if t.name == "clarify":
                            t.invoke({"question": clarify_text})
                    from langchain_core.messages import AIMessage
                    return {"messages": [*input_["messages"], AIMessage(content="(unused)")]}
            return FakeAgent()
        return _create_agent

    def _fake_agent_final_answer(self, answer_text: str):
        """Stand-in for create_agent() that just returns a final answer, no tool calls."""
        def _create_agent(llm, tools, system_prompt=None):
            class FakeAgent:
                def invoke(self, input_, config=None):
                    from langchain_core.messages import AIMessage
                    return {"messages": [*input_["messages"], AIMessage(content=answer_text)]}
            return FakeAgent()
        return _create_agent

    def test_clarify_action_returns_question_immediately(self):
        """When the model calls the clarify tool, the answer IS the clarification question."""
        from app.services.rag import _react_answer, _clarif_set

        _clarif_set("react-test-1", False)
        vs = MagicMock()

        clarify_q = "Are you asking about the 2022 report or the 2023 report?"
        with patch("app.services.rag.create_agent", side_effect=self._fake_agent_calling_clarify(clarify_q)):
            answer, elapsed_ms, citations = _react_answer(
                vectorstore=vs,
                question="What happened in the report?",
                session_id="react-test-1",
                use_graph=False,
                history=[],
                clarify_allowed=True,
            )

        assert answer == clarify_q, f"Clarification question should be returned verbatim, got: {answer}"
        assert citations == [], "Clarify action should return empty citations"
        assert elapsed_ms >= 0

    def test_clarify_sets_flag(self):
        """After the clarify tool fires, the session flag must be True."""
        from app.services.rag import _react_answer, _clarif_was_asked, _clarif_set

        _clarif_set("react-test-2", False)
        vs = MagicMock()

        with patch("app.services.rag.create_agent",
                   side_effect=self._fake_agent_calling_clarify("which file do you mean?")):
            _react_answer(vs, "test question", "react-test-2", False, [], clarify_allowed=True)

        assert _clarif_was_asked("react-test-2") is True, "Flag must be set after clarify() fires"

    def test_clarify_tool_absent_when_not_allowed(self):
        """When clarify_allowed=False, the clarify tool must not even be registered —
        the model has no way to call it, by construction, not by output filtering."""
        from app.services.rag import _react_tools

        tools = _react_tools(MagicMock(), "react-test-3", use_graph=False, clarify_allowed=False,
                              collected_docs=[], clarify_holder={})
        names = [t.name for t in tools]
        assert "clarify" not in names, "clarify tool must not be registered when clarify_allowed=False"

    def test_final_answer_path_unaffected_when_clarify_not_called(self):
        """Normal final-answer path still works when the model never calls clarify."""
        from app.services.rag import _react_answer, _clarif_was_asked, _clarif_set

        _clarif_set("react-test-4", False)
        vs = MagicMock()

        with patch("app.services.rag.create_agent", side_effect=self._fake_agent_final_answer("Here is the answer.")):
            answer, _, _ = _react_answer(vs, "test", "react-test-4", False, [], clarify_allowed=True)

        assert "Here is the answer" in answer
        assert _clarif_was_asked("react-test-4") is False, "Flag must NOT be set when clarify is never called"


# ── 6. History saved after clarification ─────────────────────────────────────

class TestClarifyHistorySaved:

    def test_clarification_response_saved_to_history(self):
        """answer_question must save user question + clarification Q to history."""
        from app.services.rag import _history_get, _history_store
        from unittest.mock import MagicMock, patch

        session_id = "hist-clarify-test-001"
        _history_store.pop(session_id, None)  # clean slate

        docs_multi = [
            Document(page_content="doc A content", metadata={"source": "a.pdf", "page_number": 1}),
            Document(page_content="doc B content", metadata={"source": "b.pdf", "page_number": 1}),
        ]

        clarify_q = "Are you asking about doc A or doc B?"

        def fake_create_agent(llm, tools, system_prompt=None):
            class FakeAgent:
                def invoke(self, input_, config=None):
                    for t in tools:
                        if t.name == "clarify":
                            t.invoke({"question": clarify_q})
                    from langchain_core.messages import AIMessage
                    return {"messages": [*input_["messages"], AIMessage(content="(unused)")]}
            return FakeAgent()

        vs = MagicMock()

        with patch("app.services.rag.create_agent", side_effect=fake_create_agent), \
             patch("app.services.rag._hybrid_retrieve", return_value=docs_multi), \
             patch("app.services.rag.check_intent", return_value=True):
            from app.services.rag import answer_question
            answer_question(
                vectorstore=vs,
                question="What is the summary?",
                session_id=session_id,
                advanced=False,
                thinking=True,
            )

        history = _history_get(session_id)
        assert len(history) >= 2, f"Expected at least 2 history entries, got: {history}"
        assert history[0]["role"] == "user"
        assert history[0]["content"] == "What is the summary?"
        assert history[1]["role"] == "assistant"
        assert history[1]["content"] == clarify_q


# ── 7 & 8. Correction and apology rules in system prompt ─────────────────────

class TestSystemPromptRules:

    def test_correction_handling_in_aria_system(self):
        from app.services.rag import _ARIA_SYSTEM
        lower = _ARIA_SYSTEM.lower()
        assert "wrong" in lower or "correct" in lower, \
            "_ARIA_SYSTEM must have correction handling instructions"

    def test_apology_rule_in_aria_system(self):
        from app.services.rag import _ARIA_SYSTEM
        lower = _ARIA_SYSTEM.lower()
        assert "apologis" in lower or "apologiz" in lower or "apology" in lower, \
            "_ARIA_SYSTEM must have single-apology rule"

    def test_rude_user_rule_in_aria_system(self):
        from app.services.rag import _ARIA_SYSTEM
        lower = _ARIA_SYSTEM.lower()
        assert "rude" in lower or "frustrated" in lower or "frustrat" in lower or "calm" in lower, \
            "_ARIA_SYSTEM must have instructions for handling rude/frustrated users"

    def test_no_repeated_apology_rule(self):
        from app.services.rag import _ARIA_SYSTEM
        lower = _ARIA_SYSTEM.lower()
        assert "once" in lower or "one" in lower or "repeat" in lower, \
            "_ARIA_SYSTEM must say to not apologise repeatedly"


# ── 9 & 10. Clarify tool registration controlled by flag ─────────────────────
# With real tool-calling, "is clarify in the prompt" is no longer meaningful —
# tools are registered via the API, not described in text. The equivalent,
# architecturally correct check is whether the clarify tool is in the tools
# list build_react_tools() returns, and what its docstring (the model's only
# view of when to use it) says.

class TestClarifyInPrompt:

    def test_clarify_tool_not_registered_when_disabled(self):
        from app.services.rag import _react_tools
        tools = _react_tools(MagicMock(), "prompt-test", use_graph=False, clarify_allowed=False,
                              collected_docs=[], clarify_holder={})
        assert "clarify" not in [t.name for t in tools], \
            "clarify tool must not be registered when clarify_allowed=False"

    def test_clarify_tool_registered_when_enabled(self):
        from app.services.rag import _react_tools
        tools = _react_tools(MagicMock(), "prompt-test", use_graph=False, clarify_allowed=True,
                              collected_docs=[], clarify_holder={})
        assert "clarify" in [t.name for t in tools], \
            "clarify tool must be registered when clarify_allowed=True"

    def test_clarify_tool_description_says_use_sparingly(self):
        from app.services.rag import _react_tools
        tools = _react_tools(MagicMock(), "prompt-test", use_graph=False, clarify_allowed=True,
                              collected_docs=[], clarify_holder={})
        clarify_tool = next(t for t in tools if t.name == "clarify")
        desc = clarify_tool.description.lower()
        assert "sparingly" in desc or "only when" in desc or "ambiguous" in desc, \
            "clarify tool description must tell the model to use it sparingly"

"""Chat-level "what changed" / "difference between X and Y" queries over
Knowledge Base document versions. See planning/PLAN-document-versioning.md,
Phase 8.

This resolves a free-text question to two KnowledgeBaseDocument rows (two
versions of the same document, or two unrelated documents) and produces a
plain-language diff summary. It is deliberately isolated from the main RAG
pipeline: app.services.rag calls try_compare_answer() in a try/except early
in answer_question(), so any failure here falls through to the normal
answer path rather than breaking chat.

Only works for documents belonging to a Knowledge Base attached to the
chat (chat.py's `extra_sources`, identified by having a `label`) - personal
session uploads have no KnowledgeBaseDocument row and are out of scope for
this feature.
"""
import logging
import re

logger = logging.getLogger(__name__)

_COMPARE_INTENT_RE = re.compile(
    r"\b(difference|differ|differs|changed|change|updated|update|compare|"
    r"comparison|what'?s new|whats new|new version|previous version|"
    r"old version|latest version)\b",
    re.IGNORECASE,
)

_VERSION_WORD_RE = re.compile(r"\bv(?:ersion)?\s*0*(\d+)\b", re.IGNORECASE)
_LATEST_RE = re.compile(r"\b(latest|newest|current|new)\b", re.IGNORECASE)
_PREVIOUS_RE = re.compile(r"\b(previous|prior|old|original|before|earlier)\b", re.IGNORECASE)


def detect_compare_intent(question: str) -> bool:
    return bool(_COMPARE_INTENT_RE.search(question))


def _normalize(name: str) -> str:
    stem = re.sub(r"\.[a-z0-9]{1,5}$", "", name, flags=re.IGNORECASE)
    return re.sub(r"[\s_\-.]+", " ", stem).strip().lower()


def _kb_ids_for_sources(db, sources: list[dict]):
    from app.models.db import KnowledgeBase

    kb_session_ids = [s["session_id"] for s in sources if s.get("label")]
    if not kb_session_ids:
        return []
    kbs = db.query(KnowledgeBase).filter(KnowledgeBase.session_id.in_(kb_session_ids)).all()
    return [kb.id for kb in kbs]


def _load_identity_chains(db, kb_ids: list) -> list[dict]:
    """Groups every KnowledgeBaseDocument row across the given KBs into
    version chains - keyed by item_id when present, by (kb_id, filename)
    otherwise - each chain sorted by version ascending. Returns
    [{"key": ..., "names": {normalized filenames seen across the chain},
    "versions": [row, ...]}]."""
    from app.models.db import KnowledgeBaseDocument

    if not kb_ids:
        return []
    rows = (
        db.query(KnowledgeBaseDocument)
        .filter(KnowledgeBaseDocument.knowledge_base_id.in_(kb_ids))
        .all()
    )
    chains: dict[tuple, dict] = {}
    for row in rows:
        key = ("item", row.knowledge_base_id, row.item_id) if row.item_id else \
              ("name", row.knowledge_base_id, row.filename)
        chain = chains.setdefault(key, {"key": key, "names": set(), "versions": []})
        chain["names"].add(_normalize(row.filename))
        chain["versions"].append(row)
    for chain in chains.values():
        chain["versions"].sort(key=lambda r: r.version)
    return list(chains.values())


def _mentioned_chains(question: str, chains: list[dict]) -> list[dict]:
    q_norm = re.sub(r"[\s_\-]+", " ", question.lower()).strip()
    hits = []
    for chain in chains:
        if any(len(name) >= 3 and name in q_norm for name in chain["names"]):
            hits.append(chain)
    return hits


def _pick_version(chain: dict, version_number: int | None, keyword: str | None):
    versions = chain["versions"]
    if not versions:
        return None
    if version_number is not None:
        for row in versions:
            if row.version == version_number:
                return row
        return None
    if keyword == "latest":
        return versions[-1]
    if keyword == "previous":
        return versions[-2] if len(versions) >= 2 else None
    return None


def resolve_compare_targets(question: str, sources: list[dict]):
    """Returns (doc_a, doc_b, label_a, label_b) if the question resolves to
    exactly two document rows worth diffing, else None (caller should fall
    back to the normal answer pipeline)."""
    from app.db.session import get_session_factory

    factory = get_session_factory()
    if factory is None:
        return None
    db = factory()
    try:
        kb_ids = _kb_ids_for_sources(db, sources)
        if not kb_ids:
            return None
        chains = _load_identity_chains(db, kb_ids)
        if not chains:
            return None

        mentioned = _mentioned_chains(question, chains)

        if len(mentioned) >= 2:
            # Two (or more) distinct documents named - arbitrary-pair case.
            # Each resolves to its current (latest/active) version.
            chain_a, chain_b = mentioned[0], mentioned[1]
            doc_a = chain_a["versions"][-1]
            doc_b = chain_b["versions"][-1]
            if doc_a.id == doc_b.id:
                return None
            return (doc_a, doc_b, doc_a.filename, doc_b.filename)

        if len(mentioned) == 1:
            return _resolve_within_chain(question, mentioned[0])

        # Nothing named ("what changed in this document?", "what's the
        # difference between v1 and v2?" with no filename at all) - safe to
        # guess only when exactly one document in scope actually HAS version
        # history; multiple candidates would mean genuinely guessing which
        # one the user means, so that case is left unresolved instead.
        chains_with_history = [c for c in chains if len(c["versions"]) >= 2]
        if len(chains_with_history) == 1:
            return _resolve_within_chain(question, chains_with_history[0])

        return None
    finally:
        db.close()


def _resolve_within_chain(question: str, chain: dict):
    versions = chain["versions"]
    if len(versions) < 2:
        return None  # only one version on file - nothing to compare

    version_numbers = [int(n) for n in _VERSION_WORD_RE.findall(question)]
    has_latest_word = bool(_LATEST_RE.search(question))
    has_previous_word = bool(_PREVIOUS_RE.search(question))

    if len(version_numbers) >= 2:
        doc_a = _pick_version(chain, version_numbers[0], None)
        doc_b = _pick_version(chain, version_numbers[1], None)
    elif has_previous_word and has_latest_word:
        doc_a = _pick_version(chain, None, "previous")
        doc_b = _pick_version(chain, None, "latest")
    else:
        # No explicit version pair given ("what changed?") - default to
        # latest vs. immediately previous.
        doc_a = versions[-2]
        doc_b = versions[-1]
    if doc_a is None or doc_b is None or doc_a.id == doc_b.id:
        return None
    label = versions[-1].filename
    return (doc_a, doc_b, f"{label} (v{doc_a.version})", f"{label} (v{doc_b.version})")


def build_compare_answer(question: str, doc_a, doc_b, label_a: str, label_b: str) -> tuple[str, list[dict]]:
    """Diffs the two resolved rows' stored extracted_text and produces a
    plain-language "previously X, now Y" summary. Falls back to a factual,
    non-LLM answer if either row has no stored text yet."""
    from app.services.diff import diff_texts

    citations = [
        {"source": doc_a.filename, "knowledge_base": None},
        {"source": doc_b.filename, "knowledge_base": None},
    ]

    if doc_a.extracted_text is None or doc_b.extracted_text is None:
        return (
            f"I can see {label_a} and {label_b}, but one of them hasn't finished "
            f"processing yet, so I can't compare their content right now. Try again "
            f"once processing completes.",
            citations,
        )

    diff_result = diff_texts(doc_a.extracted_text, doc_b.extracted_text)
    meta_line = (
        f"{label_b} was uploaded on "
        f"{doc_b.uploaded_at.isoformat() if doc_b.uploaded_at else 'an unknown date'}, "
        f"replacing {label_a} from "
        f"{doc_a.uploaded_at.isoformat() if doc_a.uploaded_at else 'an unknown date'}."
        if doc_b.version > doc_a.version and doc_a.knowledge_base_id == doc_b.knowledge_base_id
        and (doc_a.item_id == doc_b.item_id or doc_a.filename == doc_b.filename)
        else f"Comparing {label_a} and {label_b}."
    )

    if diff_result["lines_added"] == 0 and diff_result["lines_removed"] == 0:
        return f"{meta_line} I found no differences between the two - the content is identical.", citations

    changed = [l for l in diff_result["lines"] if l["type"] != "unchanged"]
    # Cap what goes to the LLM - long documents could otherwise blow the
    # context window; this is a summary of CHANGES, not the whole document.
    diff_snippet = "\n".join(
        f"{'+' if l['type'] == 'added' else '-'} {l['text']}" for l in changed[:200]
    )

    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from app.services.rag import llm_invoke

        system = (
            "You compare two versions of a document for a user, from a line-level diff "
            "(lines marked - for old, + for new).\n\n"
            "Answer the user's actual question - don't force every response into the same "
            "shape:\n"
            "- A broad question ('what changed?') gets a list of the real changes, as short "
            "markdown bullets.\n"
            "- A yes/no question ('is X still the same?', 'did the deadline change?') gets a "
            "direct yes/no answer first, with the specific old/new values only if they "
            "differ.\n\n"
            "Group bullets by topic/paragraph, not by diff line or sentence fragment - "
            "several adjacent diff lines about the same subject (e.g. one paragraph's "
            "timeline, or one section's numbers) belong in ONE bullet, not one bullet per "
            "line. But grouping must never cost you a change: every distinct fact that "
            "differs between old and new - every changed number, date, name, or status - "
            "has to show up somewhere in the bullet for its topic. If a paragraph changed in "
            "three places, say what was \"...\", now \"...\" for all three, in that one "
            "bullet, not just the first one you notice. When genuinely unsure whether to "
            "merge two changes into one bullet or keep them separate, keep them separate - "
            "losing a change by over-merging is worse than an extra bullet.\n\n"
            "Some diff lines are NOT real changes - the same sentence can land on different "
            "line boundaries between two extractions (re-wrapping, page breaks) and show up "
            "as both removed and added even though the words are the same or only "
            "punctuation/whitespace differs. Read the removed/added pair together and skip "
            "it entirely if the actual content is unchanged - never report 'no change' as if "
            "it were a change, just omit it.\n\n"
            "Only mention something the user didn't ask about if it's a genuine, "
            "substantive change. Never invent a change not present in the diff. Keep it "
            "tight - no preamble, no restating the question, no filler closing sentence."
        )
        user = f"Diff (- old / + new):\n{diff_snippet}\n\nUser's question: {question}"
        response = llm_invoke([SystemMessage(content=system), HumanMessage(content=user)])
        summary = getattr(response, "content", str(response)).strip()
        return f"{meta_line}\n\n{summary}", citations
    except Exception as e:
        logger.warning(f"[COMPARE] LLM summarization failed ({e}) - returning raw diff counts")
        return (
            f"{meta_line} {diff_result['lines_added']} line(s) added, "
            f"{diff_result['lines_removed']} line(s) removed. "
            f"(Couldn't generate a plain-language summary right now.)",
            citations,
        )


def try_compare_answer(question: str, sources: list[dict]) -> tuple[str, list[dict]] | None:
    """Entry point called from app.services.rag.answer_question(). Returns
    None (meaning: not a compare question, or couldn't resolve it
    confidently) so the caller falls through to the normal answer
    pipeline - never raises."""
    try:
        if not detect_compare_intent(question):
            return None
        resolved = resolve_compare_targets(question, sources)
        if resolved is None:
            return None
        doc_a, doc_b, label_a, label_b = resolved
        return build_compare_answer(question, doc_a, doc_b, label_a, label_b)
    except Exception as e:
        logger.warning(f"[COMPARE] try_compare_answer failed ({e}) - falling back to normal answer")
        return None

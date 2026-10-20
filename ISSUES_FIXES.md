# ARIA - Issues & Fixes Log
**Session date:** 2026-06-29

---

## Plans & Design Decisions Made Today

### Plan 1 - Ambiguity & Clarification Feature

**Problem stated:** When user uploads two case files and asks "who is the victim?" or "what happened in 2007?", ARIA picks one document and answers without checking if the question is even clear enough to answer.

**Approach decided:**
- Build this in **Thinking Mode only** - standard mode stays fast, no clarification there
- **No new LangChain Tool objects** - `Clarify` is a plain text action in the ReAct prompt, same pattern as `vector_search`. No tool binding, no AgentExecutor changes.
- The LLM is NOT trusted alone to decide when to clarify - it over-asks. Three **code-level gates** must all pass first:
  - Gate 1: retrieved docs must come from **2+ distinct source files** (code check, not LLM)
  - Gate 2: ARIA must not have already asked a clarification in the previous turn (flag check)
  - Only after both gates pass → LLM gets `clarify()` in its prompt and can decide
- `Clarify` is a **terminal action** - when the agent says `Action: clarify(question)`, the loop stops immediately. Same behaviour as `Final Answer`.
- The clarification question is saved to conversation history as the assistant turn - so the user's follow-up reply has full context.

**What was ruled out and why:**
- Pre-check function before ReAct (`_check_ambiguity()`) - dropped because it adds an extra LLM call even when not needed. Gates handle this with zero LLM cost.
- Word-count gate (skip clarify for short questions) - dropped because "who is the victim?" is 4 words and IS a valid ambiguous query.
- Standard mode clarification - not built. Standard mode is for speed. Complex multi-doc ambiguity warrants Thinking Mode.

---

### Plan 2 - Correction & Frustration Handling

**Problem stated:** When a user says "that's wrong" or "that's not what I asked" (or gets abusive), ARIA should only apologise when it actually made a mistake - not every time. And it should never apologise more than once.

**Approach decided:** System prompt rules only - no separate detection function, no extra LLM call. The ReAct agent already receives full conversation history, so it can reason about whether its previous answer was correct.

**Five rules added to `_ARIA_SYSTEM`:**
1. If user says previous answer was wrong → re-read history, re-search, correct yourself
2. If you were wrong → acknowledge once clearly, do not repeat the apology
3. If user is rude but your answer was factually correct → stay calm, do not apologise, clarify what the documents say
4. Never apologise more than once per mistake
5. If user asks something unrelated to uploaded documents → politely redirect

**What was ruled out:** Separate correction-detector function with its own LLM call - unnecessary overhead. The ReAct agent with history already has all the information to reason about this correctly.

---

### Plan 3 - Server Warmup Strategy

**Problem stated:** First user after server restart always waits 10–25 seconds extra due to cold starts on embedding model, reranker, BM25 index, and LiteParse.

**Approach decided:** Pre-warm everything at startup in parallel using `asyncio.gather`. Four tasks:
- `_warmup_embeddings()` - dummy `embed_query("warmup")` call → HF API connection established
- `_warmup_bm25()` - build BM25 index for the default session → in RAM, ready instantly
- `_warmup_reranker()` - dummy `sentence_similarity` call → HF reranker connection established
- `_warmup_parser()` - parse a minimal dummy PDF → LiteParse internal models initialised

All four run in parallel (not sequential) to minimise startup time. Each wrapped in `try/except` - any failure is logged as a warning and never blocks the server from starting.

**Runs after:** `_seed_default_session()` (the sample doc must be in the vector store before BM25 can be built from it).

---

### Plan 4 - Document Extraction Speed

**Problem stated:** PDF uploads were slow and blocked other users during parsing.

**Root causes identified:**
1. `_parser.parse()` (LiteParse, CPU-bound) runs directly in the async event loop - freezes all other requests during any upload
2. Embedding batches: 32 chunks per call, sequential - 100-chunk doc = 4 serial HF API round-trips
3. LiteParse had no warmup - first PDF after restart always slowest

**Approach decided:**

| Problem | Fix | Why this way |
|---|---|---|
| Event loop blocked | `await asyncio.to_thread(_parser.parse, tmp_path)` | Moves CPU work to thread pool, event loop stays free |
| Sequential batches | `_add_texts_batched_parallel()` with `asyncio.Semaphore(3)` | 3 concurrent batches safe for HF API rate limits |
| Batch size too small | Raised from 32 → 50 chunks per call | Fewer round-trips, HF API handles 50 easily |
| LiteParse cold start | `_warmup_parser()` at startup | Parse once at boot, not at first user upload |

**Why semaphore at 3 and not more:** HuggingFace free inference API has rate limits. 3 concurrent requests is aggressive enough to get ~2–3× speedup without hitting rate-limit errors.

**Why sequential insert kept for seeding:** `_add_texts_batched` (sequential) is still used in `_seed_default_session()` at startup - seeding runs once and correctness matters more than speed there.

---

### Plan 5 - dedup_threshold_mb Reasoning

**What it controls:** Files below the threshold skip the TF-IDF similarity check and only do the fast SHA-256 hash check. Files above go through both.

**Why 0 was wrong:** With `threshold=0`, the condition `file_size < 0` is never true, so **every file** goes through TF-IDF regardless of size. A 1 KB TXT file took as long as a 10 MB PDF in the dedup step.

**Why 2 is right:** Almost all user uploads (short contracts, reports, notes) are under 2 MB. They only need the hash check - TF-IDF is for catching near-duplicate large documents that weren't caught by exact hash match. Setting to 2 means small files are processed fast, large files still get the full similarity check.

---

## 1. Ambiguity / Clarification Feature (Thinking Mode)

### Problem
When a user uploads multiple documents (e.g. two case files) and asks a vague question like "who is the victim?" or "what happened in 2007?", ARIA would pick one document arbitrarily and answer without telling the user it was guessing. No mechanism existed to ask the user for clarification.

### Risk with naive solution
If the LLM is trusted to decide when to ask for clarification, it over-asks - triggering on clear questions, repeating itself, and annoying users.

### Design decisions
- **Thinking Mode only** - standard mode stays fast with no clarification
- **No new LangChain Tool objects** - `Clarify` is a text action in the ReAct prompt, same as `vector_search`
- **Three hard code-level gates** (not LLM-decided) must all pass before the LLM is even allowed to use `Clarify`:
  - Gate 1: `distinct_sources >= 2` - only possible when 2+ different documents exist in results
  - Gate 2: `_clarif_was_asked(session_id)` flag - if ARIA already asked a clarification last turn, blocked
  - If either gate fails → `clarify_allowed=False` → `clarify()` not even shown in prompt
- **Terminal action** - `Clarify` immediately ends the ReAct loop (like `Final Answer`), sets a session flag, returns the question as the response
- **History saved** - clarification Q&A saved to conversation history so the follow-up has full context

### Files changed
- `app/services/rag.py`
  - `_ARIA_SYSTEM` - added correction handling + single apology rules
  - `_clarif_store`, `_clarif_was_asked()`, `_clarif_set()` - new session flag store
  - `_react_answer()` - added `clarify_allowed` parameter, `clarify()` action parser (terminal), conditional Rules section in prompt
  - `answer_question()` - added Gate 1 + Gate 2 checks before calling `_react_answer`

### Also fixed in this feature
- **Correction handling:** When user says "that's wrong" or "that's not what I asked", ARIA re-reads history, re-searches, and corrects itself. Apologises once clearly - never repeatedly. If user was rude but ARIA was actually correct, it stays calm and clarifies without apologising.
- **Clarify rules in prompt were unconditional:** The Rules section mentioned `clarify()` even when `clarify_allowed=False`. Fixed - Rules section is now conditional on the flag.

---

## 2. Test Suite Fixes

### Issue A - `test_config.py` stale expectations (2 failures)
**Root cause:** `config.yaml` was updated but tests expected old values.
- `groq_model` expected `llama-3.3-70b-versatile` - actual value: `llama-3.1-8b-instant`
- `dedup_threshold_mb` expected `2.0` - actual value at time of test: `0`

**Fix:** Updated `test_config.py` to match actual config values.

---

### Issue B - `test_upload.py::test_exact_max_files_passes_validation` hung indefinitely
**Root cause:** The test uploaded 5 files all with identical content `b"hello world"` to the same `client_token`. After the first file was processed:
1. `dedup_svc.update_cache()` cached the file hash with `chunks > 0`
2. Files 2–5 all hit the in-memory cache → `DedupResult("confirm", ...)`
3. The upload SSE stream called `asyncio.wait_for(gate.wait(), timeout=60)` for each file
4. No one called the confirm endpoint → **each file waited 60 seconds** → test hung for ~4 minutes

**Fix:** `test_upload.py` - each file now has unique content (`f"unique content for file {i}"`) + `_mem_cache.clear()` before the test. Test uses `client.stream()` (streaming context) instead of `client.post()` for SSE compatibility.

---

### Issue C - New test file: `test_clarification.py` (26 tests)
**Added tests covering:**
- Clarification flag store: `_clarif_was_asked`, `_clarif_set`, independence per session
- Gate 1: single source blocks clarification, 2+ sources allows it
- Gate 2: already-clarified flag blocks second ask
- Combined gates: all combinations of pass/fail
- ReAct Clarify terminal action: returns question immediately, sets flag, is blocked when `clarify_allowed=False`
- History saved after clarification response
- `_ARIA_SYSTEM` contains correction rules, apology rule, rude-user handling rule
- `clarify()` tool absent from prompt when disabled, present when enabled, prompt says "use sparingly"

---

## 3. `dedup_threshold_mb` Config Fix

### Problem
`config.yaml` had `dedup_threshold_mb: 0`. With threshold = 0, `file_size < 0` is never true, so **every file** - even a 1 KB TXT - goes through the full TF-IDF similarity check. TF-IDF is the slow part of dedup.

### What the threshold is for
Files **below** the threshold skip TF-IDF (only do the fast SHA-256 hash check). Files **above** the threshold do SHA-256 + TF-IDF. Setting it to 0 defeated this optimisation entirely.

### Fix
`config.yaml`: `dedup_threshold_mb: 2` - files under 2 MB skip TF-IDF.
`test_config.py`: updated expected value back to `2.0`.

---

## 4. Server Warmup - Eliminate Cold Starts

### Problem
After every server restart, the first user request was slow because:
- HuggingFace embedding model connection not yet established (~2–5s)
- BM25 index not yet built for the default session (~1–2s)
- HuggingFace reranker connection not yet established (~2–3s)
- LiteParse had not yet initialised its internal models (~3–8s on first PDF)

### Fix
Added four warmup functions to `app/main.py`, all run in parallel at startup via `asyncio.gather`:

| Function | What it does |
|---|---|
| `_warmup_embeddings()` | Calls `embed_query("warmup")` - establishes HF API connection |
| `_warmup_bm25()` | Calls `_get_or_build_bm25(default_session)` - BM25 index ready in RAM |
| `_warmup_reranker()` | Calls `sentence_similarity("warmup", ...)` - HF reranker connection live |
| `_warmup_parser()` | Parses a minimal dummy PDF - LiteParse fully initialised |

All failures are caught and logged as warnings - warmup never blocks server startup.

**Startup sequence after fix:**
```
Server starting...
→ _seed_default_session()   (sample doc seeded, skips if already done)
→ _warmup() - 4 tasks run in parallel:
      ├── embedding ready
      ├── BM25 index built
      ├── reranker ready
      └── LiteParse ready
→ "Server ready" - zero cold starts for any user
```

---

## 5. Document Extraction Speed

### Problem A - `parse_file()` blocked the entire event loop
`_parser.parse(tmp_path)` (LiteParse, CPU-bound) was called directly in `async def parse_file()` with no thread offload. During any PDF upload, the event loop was frozen - all other users' chat requests stalled until parsing completed.

**Fix:** `app/services/ingestion.py`
```python
# Before
result = _parser.parse(tmp_path)

# After
result = await asyncio.to_thread(_parser.parse, tmp_path)
```
LiteParse now runs in the thread pool. Event loop stays free during uploads.

---

### Problem B - Embedding batches were sequential and undersized
`_add_texts_batched()` sent 32 chunks per HF API call, one batch at a time. For a large PDF with 100 chunks: 4 serial API round-trips, each waiting for the previous to finish.

**Fix:** `app/routers/upload.py`
- `EMBED_BATCH_SIZE` raised from `32` → `50` (fewer round-trips)
- New `_add_texts_batched_parallel()` - fires up to 3 batches simultaneously using `asyncio.Semaphore(3)` + `asyncio.gather`
- Main upload path uses `_add_texts_batched_parallel` (the old sequential `_add_texts_batched` kept for startup seeding only)

**Impact:** 100-chunk doc = 2 batches running in parallel → approximately 2× faster embedding step.

---

## Summary Table

| # | Issue | File(s) | Status |
|---|---|---|---|
| 1 | No clarification when query ambiguous across docs | `rag.py` | Fixed |
| 1a | Correction handling - ARIA re-checks when told it's wrong | `rag.py` | Fixed |
| 1b | `clarify()` rules appeared in prompt even when disabled | `rag.py` | Fixed |
| 2A | Stale test expectations for `groq_model` and `dedup_threshold_mb` | `test_config.py` | Fixed |
| 2B | Upload test hung indefinitely (dedup confirm gate, 60s × 4 files) | `test_upload.py` | Fixed |
| 2C | No tests for clarification feature | `test_clarification.py` | Added (26 tests) |
| 3 | `dedup_threshold_mb: 0` made all files go through slow TF-IDF | `config.yaml` | Fixed (restored to 2) |
| 4 | Cold starts on first request after server restart | `main.py` | Fixed (parallel warmup) |
| 5A | `parse_file()` blocked event loop during PDF parsing | `ingestion.py` | Fixed (thread pool) |
| 5B | Sequential embedding batches, undersized (32 chunks) | `upload.py` | Fixed (50 chunks, 3 parallel) |

# ARIA - Issues & Fixes Log
**Session date:** 2026-06-29

---

# Session date: 2026-10-20 - Source Connector Integration & Security

Scope: integrating CONNECTOR_POC's 10+1 source-system connectors (Gmail, Google
Drive, GitHub, Jira, ServiceNow, Salesforce, SharePoint, Teams, Azure DevOps,
File Share, LMS) into the real app, end to end - backend, frontend, security,
and the refactor into a standalone package.

---

## 1. Per-user token isolation (security)

### Problem
CONNECTOR_POC stored each OAuth connector's token (Gmail, Google Drive,
Salesforce) in a single flat file on disk (e.g. `.gmail_token.json`) with no
concept of "whose" it was. In a multi-user app, whichever user connected
Gmail first became *the* Gmail connection for every user - User B would see
User A's inbox.

### Fix
New `connector_tokens` DB table (`user_id`, `provider`, `access_token`,
`refresh_token`, `expires_at`, `extra`), unique on `(user_id, provider)`.
Every connector call now looks up the token scoped to the authenticated
user making the request, never a shared file.

### Files changed
- `app/connectors/models.py` (`ConnectorToken` model)
- `app/connectors/token_store.py`
- `alembic/versions/003_connector_tokens.py`

---

## 2. GitHub connector depended on a local `gh` CLI login

### Problem
The original connector shelled out to `gh auth token` to get a GitHub token -
works on a developer's own machine where `gh` is logged in, but has no
equivalent in a deployed container, and isn't tied to any specific user.

### Fix
Replaced with a plain `GITHUB_TOKEN` env var, read directly - no subprocess,
no CLI dependency.

### Files changed
- `app/connectors/clients/github.py`

---

## 3. Connector "configured" status falsely reported placeholders as real

### Problem
The first version of `GET /connectors/status` only checked that an env var
was non-empty. Since every connector's `.env` entry was pre-filled with
placeholder text (e.g. `GITHUB_TOKEN=your-github-personal-access-token`),
every connector showed as "Connected" when nothing was actually configured.

### Fix
Added `_is_placeholder()` - flags any value still containing `your-`, and for
File Share, additionally verifies the directory actually exists on disk.
Re-verified live: went from a false "8/11 connected" down to the correct
"1/11" (only the no-config-needed LMS mock).

### Files changed
- `app/connectors/router.py`

---

## 4. Embedding failures during connector ingest crashed with a raw 500

### Problem
`POST /connectors/{provider}/ingest` had no try/except around the embedding
call. When the HuggingFace account ran out of inference credits mid-test,
the endpoint returned a bare "Internal Server Error" instead of a usable
message - the same failure the normal `/upload/files` path already handles
cleanly.

### Fix
Wrapped the embed step in try/except, returning
`"Failed to embed {filename}. Check your embedding provider (e.g. HuggingFace
credits) and try again."` with a 502, matching the existing upload behaviour.
Verified live: same failure now returns the clean message instead of a crash.

### Files changed
- `app/connectors/pipeline_bridge.py`

---

## 5. OAuth redirect URI port mismatch broke Gmail/Drive/Salesforce login

### Problem
`.env` had `GOOGLE_REDIRECT_URI` / `GOOGLE_DRIVE_REDIRECT_URI` /
`SALESFORCE_REDIRECT_URI` pointing at port 8000, but the app actually runs on
port 8001. Google/Salesforce would redirect the browser back to a port
nothing was listening on, breaking the login flow before it could even start.

### Fix
Updated all three to port 8001 to match the real running server; documented
that whoever registers the OAuth app in Google Cloud Console / Salesforce
must register the exact matching URI.

### Files changed
- `.env`

---

## 6. Salesforce OAuth rejected with "missing required code challenge"

### Problem
Salesforce's Connected App requires PKCE (`code_challenge` /
`code_verifier`) on the Web Server OAuth flow; the connector never sent one.

### Fix
Added `_new_pkce_pair()` (S256 challenge/verifier, RFC 7636), stored the
verifier in a short-lived in-memory map keyed by user_id during the
authorization step, and sent it back on token exchange.

### Files changed
- `app/connectors/clients/salesforce.py`

---

## 7. Gmail / Google Drive silently capped results with no way to see more

### Problem
The browse popups fetched a hardcoded `max_results=20` (Gmail) / `100`
(Google Drive) with no pagination at all - if you had more messages/files
than that, the rest were simply invisible, with no indication anything was
missing.

### Fix
Added `page_token` support end to end (connector client → router → frontend)
and a "Load more" button that appends the next batch instead of replacing
the list, using the real `next_page_token` the Gmail/Drive APIs already
return.

### Files changed
- `app/connectors/clients/gmail.py`, `google_drive.py`
- `app/connectors/router.py`
- `app/static/chat.js`

---

## 8. UI: hamburger sidebar toggle overlapped the Connect panel

### Problem
`.sidebar-hamburger` is `position: fixed` at a coordinate that physically
overlapped the top-right ~24x18px corner of whatever sat at the top of the
sidebar (the Connect panel's info note, and the Files/URL/API tabs row).
Confirmed by measuring actual DOM rects in a live browser, not just visually.

### Fix
Increased `.sidebar`'s top padding so its content always clears the
hamburger button's full footprint. Re-measured after the fix: clean 8px gap,
no overlap.

### Files changed
- `app/static/style.css`

---

## 9. UI: zero gap between the Connect panel's note and the connector list

### Problem
`#connectors-section` had no layout/gap rule of its own, so its note bar and
the connector list sat flush against each other with 0px spacing - measured
directly in the DOM, confirmed.

### Fix
Added `display:flex; flex-direction:column; gap:10px` to `#connectors-section`,
matching the spacing already used in the Files/URL/API tabs panel.

### Files changed
- `app/static/style.css`

---

## 10. UI: "Browse" button was white for some connectors, purple for others

### Problem
The button got a `secondary` (white/outlined) CSS class specifically for
Gmail/Google Drive/Salesforce once connected, while every other connector's
"Browse" stayed solid purple - an arbitrary inconsistency with no reason
behind it.

### Fix
Removed the `secondary` variant entirely; every enabled Browse/Connect
button is now the same solid purple, the only other state being the greyed
out "not configured" disabled look.

### Files changed
- `app/static/chat.js`, `style.css`

---

## 11. Dev server stuck in a near-continuous self-restart loop

### Problem
`uvicorn --reload` was watching the whole project directory, including
`logs/app.log`. Every log line written re-triggered the file watcher, which
logged another "change detected" line, which re-triggered itself - a
feedback loop causing a full server restart (and full warmup: embeddings,
Docling models, etc.) roughly every 20-25 seconds, discovered by noticing
30,000+ near-identical log lines.

### Fix
`--reload-exclude "logs/*"` added to the documented run command.

### Files changed
- `README.md`

---

## 12. Connector code was tangled into the rest of the app

### Problem
Connector clients, the router, and the OAuth token model were scattered
across `app/services/connectors/`, `app/routers/connectors.py`, and
`app/models/db.py` alongside unrelated app models - hard to hand off to a
different project without untangling RAG-pipeline-specific code from the
connector logic.

### Fix
Extracted everything into a single self-contained `app/connectors/` package
(`clients/`, `token_store.py`, `models.py` with its own independent
`declarative_base()`, `router.py`, and `access_gate.py`), with exactly one
file - `pipeline_bridge.py` - allowed to import this app's RAG internals.
A different host app only needs to rewrite that one file to reuse the whole
package. Verified afterward: route count unchanged (50 `/connectors/*`
paths before and after), and a real ingest-then-chat round trip re-tested
live to confirm the pipeline extraction changed nothing behaviourally.

### Files changed
- New `app/connectors/` package; deleted old `app/services/connectors/` and
  `app/routers/connectors.py`; `app/main.py`, `app/models/db.py` updated

---

## 13. Six connectors had no browse UI at all

### Problem
SharePoint, Microsoft Teams, Azure DevOps, Jira, ServiceNow, and Salesforce
showed a "browsing UI isn't built yet" placeholder - only File Share, LMS,
GitHub, Gmail, and Google Drive had working pickers.

### Fix
Built real nested browse-and-pick popups for all six, reading each
connector's actual field shapes from source rather than guessing by analogy
(SharePoint/Azure DevOps return raw vendor API shapes; Jira/ServiceNow/
Salesforce are normalized - genuinely different). Live-verified against real
accounts where credentials existed (Jira, Azure DevOps); confirmed clean
error handling (not crashes) where credentials were still placeholders
(ServiceNow, Teams) or OAuth wasn't yet completed (Salesforce).

### Files changed
- `app/static/chat.js`

---

## 14. No access control beyond normal login for connecting real third-party accounts

### Problem
Any logged-in user (including a guest) could connect Gmail/GitHub/Jira/
Salesforce with no additional gate - risky given a connected session is real
access to someone's real external account, not just app data.

### Fix
Added a second, independent lock: `CONNECTORS_ACCESS_KEY` (freshly generated
random secret, `.env`-only). Every `/connectors/*` route now requires a
short-lived, per-user signed "unlock token" obtained by first submitting the
real key via `POST /connectors/unlock`; comparison is constant-time
(`hmac.compare_digest`) so a wrong guess can't be timed. Verified live:
locked by default (403), wrong key rejected (403, with the raw key never
stored/echoed), correct key unlocks, a different user's token is rejected
even if stolen (scoped to `user_id` in the signed payload).

### Files changed
- `app/connectors/access_gate.py` (new)
- `app/connectors/router.py`, `app/main.py`
- `app/static/chat.js`, `style.css`, `index.html`

---

## 15. Access-key bug: a wrong key logged the user out of their whole account

### Problem
The first version returned `401` for "incorrect access key." This frontend
already treats any `401` as "your login session is invalid" and force-logs-
out the user - so typing the wrong connectors key (a completely separate,
lower-stakes thing) was logging people out of ARIA entirely. Caught live
during testing, not just in review.

### Fix
Changed "incorrect access key" to `403` (forbidden, not unauthorized) so it
can never collide with the login-session check. Re-tested after the fix:
wrong key now shows a clean inline error with the user still logged in.

### Files changed
- `app/connectors/router.py`

---

## 16. `.md` files were rejected by both upload and connector ingestion

### Problem
`ALLOWED_EXTENSIONS` only included `.pdf/.doc/.docx/.txt`; Markdown files
from GitHub, File Share, or a direct upload were rejected outright, and even
if allowed, would have been wrongly routed through the PDF/DOCX document
parser instead of read as plain text.

### Fix
Added `.md` to `ALLOWED_EXTENSIONS` (shared by regular upload and the
connector ingest bridge) and to `parse_file()`'s plain-text fast path
(same as `.txt`). Verified live: uploaded a real `.md` file, confirmed it
parsed as raw text (not garbled through Docling), embedded, and answered a
follow-up chat question with a correct citation.

### Files changed
- `app/routers/upload.py`, `app/services/ingestion.py`
- `app/static/chat.js`, `index.html`, `auth.html`

---

## 17. Chat session is lost on page refresh (diagnosed, fix in progress)

### Problem
`state.sessionId` (which session the chat box talks to) exists only in
in-memory JS state - never written to `localStorage`. A page refresh or new
tab silently resets it to a brand-new empty session with no warning. Traced
a real user report ("I asked about lesson 2 and it said it had no
information") to exactly this: the file had been ingested successfully
(confirmed via a direct DB query and a direct API call against the real
session, which answered correctly with a citation) - the chat UI was simply
asking the wrong, newly-generated empty session instead of the one the file
actually lived in.

### Status
Root cause confirmed and reproduced. Fix (persist the active session id to
`localStorage`, mirroring how the login token already persists) identified
but not yet implemented.

---

## Summary Table

| # | Issue | File(s) | Status |
|---|---|---|---|
| 1 | Shared OAuth token file leaked one user's connection to all users | `token_store.py`, `models.py` | Fixed |
| 2 | GitHub connector depended on local `gh` CLI login | `github.py` | Fixed |
| 3 | Placeholder env values falsely reported as "configured" | `router.py` | Fixed |
| 4 | Embedding failure during ingest crashed with raw 500 | `pipeline_bridge.py` | Fixed |
| 5 | OAuth redirect URIs pointed at the wrong port | `.env` | Fixed |
| 6 | Salesforce OAuth missing required PKCE code challenge | `salesforce.py` | Fixed |
| 7 | Gmail/Drive silently capped results, no pagination | `gmail.py`, `google_drive.py`, `chat.js` | Fixed |
| 8 | Hamburger button overlapped the Connect panel | `style.css` | Fixed |
| 9 | Zero spacing between Connect panel note and list | `style.css` | Fixed |
| 10 | Inconsistent Browse button colors across connectors | `chat.js`, `style.css` | Fixed |
| 11 | Dev server stuck in a self-triggered restart loop | `README.md` | Fixed |
| 12 | Connector code tangled into the rest of the app | `app/connectors/*` | Fixed (refactor) |
| 13 | 6 of 11 connectors had no browse UI | `chat.js` | Fixed |
| 14 | No extra access control for connecting real third-party accounts | `access_gate.py`, `router.py` | Fixed |
| 15 | Wrong access key logged the user out of their whole account | `router.py` | Fixed |
| 16 | `.md` files rejected by upload and ingestion | `upload.py`, `ingestion.py` | Fixed |
| 17 | Chat session lost on page refresh, misattributed as a RAG bug | `chat.js` | Diagnosed, fix pending |

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

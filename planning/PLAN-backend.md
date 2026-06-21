# Backend Plan — Python / FastAPI

---

## Phase 1 — uv + Docker Foundation ✅ Done

- `pyproject.toml` + `uv.lock` replace `requirements.txt`
- `Dockerfile` — Python 3.13-slim + uv, non-root user (UID 1000) for HF Spaces
- `PLAYWRIGHT_BROWSERS_PATH` set to `/app/.playwright-browsers` so non-root user can access it
- Anyone clones and runs — no manual install needed

---

## Phase 2 — FastAPI Backend ✅ Done

### Folder Structure
```
app/
├── main.py              # FastAPI app, mounts routers + static files
├── routers/
│   ├── upload.py        # POST /upload/files, /upload/url, /upload/api
│   └── chat.py          # POST /chat
├── services/
│   ├── rag.py           # LLM, chunking, retrieval, answer pipeline
│   ├── ingestion.py     # File parsing, URL scraping, API fetching
│   └── vector_store.py  # Embedding + vector store with fallback
├── models/
│   └── schemas.py       # Pydantic request/response models
└── static/              # Frontend served by FastAPI
```

### API Endpoints
```
GET  /health             → { status: "ok" }
POST /upload/files       → SSE stream — parses files, chunks, embeds, returns session_id
POST /upload/url         → SSE stream — scrapes URL, chunks, embeds, returns session_id
POST /upload/api         → JSON — fetches API endpoint, chunks, embeds, returns session_id
POST /chat/              → { session_id, question } → { answer, citations, elapsed_ms }
GET  /                   → serves frontend (index.html)
```

### RAG Pipeline
```
1. INGESTION          2. STORING           3. RETRIEVAL         4. LLM ANSWER
   Files / URLs /        ChromaDB (free)      MMR search           Groq llama-3.3-70b
   APIs parsed &         pgvector             diverse, non-        answers from
   chunked (1000         (Supabase)           redundant chunks     retrieved context
   chars, 200 overlap)   per session_id
```

### Ingestion — Supported Sources
| Source | How | Library |
|---|---|---|
| PDF, DOCX, TXT | Parse with page metadata | LiteParse (Rust) |
| URL | Scrape content | Crawl4AI (primary) → Playwright (fallback) |
| API endpoint | HTTP GET + JSON flatten | httpx |

### LLM — Groq (Free)
- Model: `llama-3.3-70b-versatile` (default, configurable via `GROQ_MODEL`)
- Free tier: 14,400 requests/day, 6,000 tokens/minute
- Switch model anytime via `.env` — no code change needed

### Embeddings — Dual Provider
| Provider | Model | Dims | Cost |
|---|---|---|---|
| **BGE (default)** | BAAI/bge-large-en-v1.5 | 1024 | Free (HF Inference API) |
| OpenAI (fallback) | text-embedding-3-small | 1536 | Paid |

Switch via `EMBEDDING_PROVIDER=bge` or `openai` in `.env`.

### Vector Store — Dual Provider
| Store | When used | Persistence |
|---|---|---|
| **ChromaDB** | Default / HF Spaces / no DB creds | Local disk (`./chroma_data/`) |
| pgvector (Supabase) | When `DB_HOST` is set | Cloud PostgreSQL |

Auto-fallback: tries pgvector first, falls back to ChromaDB if connection fails.

### Retrieval — MMR
Tunable via `.env`:
```env
RETRIEVAL_K=3          # chunks returned to LLM
RETRIEVAL_FETCH_K=10   # candidate pool size (must be >= K)
RETRIEVAL_LAMBDA=0.7   # 1.0=similarity, 0.0=diversity, 0.7=balanced
```

---

## Phase 3 — SSE Streaming Upload ✅ Done

File and URL uploads stream real-time progress to the frontend via Server-Sent Events:
- `step start` → shows step starting with live timer
- `step done` → marks step complete with elapsed time
- `complete` → returns `session_id`, total time
- `error` → surfaces failure reason

---

## Phase 4 — Page Tracking + Confidence ✅ Done

Each chunk stored in the vector store carries:
```json
{
  "source": "report.pdf",
  "chunk_index": 7,
  "page_number": 3,
  "confidence": 0.9812
}
```

How it works:
- `parse_file()` returns `(full_text, page_spans)` — list of `{page_number, start, end, confidence}`
- `confidence` = avg OCR confidence across all text items on that page (from LiteParse)
- `chunk_text_with_offsets()` returns chunks with their `start_index` in the full text
- `find_page(start_index, page_spans)` maps each chunk to its page
- Citations in chat responses include `page_number` and `confidence`

---

## Phase 5 — Smart Deduplication 📋 Planned

> Hash-based re-embed is implemented in `upload.py`. TF-IDF 3-point check + confirmation gate are still planned.

Avoid re-creating embeddings when the same file is uploaded again. Saves processing time and cost.

### Privacy Isolation — X-Client-Token
Every browser generates a UUID on first page load and stores it in `localStorage`.
Sent as `X-Client-Token` header on every upload request.
All dedup lookups are scoped to this token — different browsers never share cached data.

```javascript
// chat.js — on page load
if (!localStorage.getItem('client_token')) {
    localStorage.setItem('client_token', crypto.randomUUID())
}

// sent with every upload
fetch('/upload/files', {
    headers: { 'X-Client-Token': localStorage.getItem('client_token') },
    body: form
})
```

Without this: User A uploads `report.pdf` → User B uploads same filename → hits User A's cache → privacy breach.
With this: every browser is completely isolated.

---

### What gets stored in `Document` table

| Field | Purpose |
|---|---|
| `client_token` | Browser UUID — isolates dedup per browser, prevents privacy leaks |
| `filename` | Normalized to lowercase — case-insensitive matching |
| `file_size` | Size in bytes |
| `content_hash` | SHA256 of file bytes — bulletproof exact match |
| `first_chunk` | First 500 chars of parsed text |
| `middle_chunk` | Middle 500 chars of parsed text |
| `last_chunk` | Last 500 chars of parsed text |
| `avg_confidence` | Avg OCR confidence from LiteParse — sets dynamic TF-IDF threshold |
| `chunks_stored` | Total chunks written to vector store — shown in confirmation card |
| `session_id` | Reuse this if duplicate detected |
| `status` | `"pending"` → `"complete"` → `"failed"` — never deleted, shown to user |
| `uploaded_at` | Timestamp — shown as "uploaded 2 hours ago" in confirmation card |

**DB constraints:**
- Unique constraint on `(client_token, content_hash)` — prevents race condition duplicates
- Index on `client_token` — fast lookup for TF-IDF corpus queries

**Status values:**
- `pending` — processing in progress
- `complete` — fully processed, safe to reuse
- `failed` — processing failed, shown to user as failed upload, never reused

---

### Deduplication Rules (in order)

**Rule 1 — Same filename + same file size + same SHA256 hash → Reuse immediately**
- Compute SHA256 of file bytes (~5ms, negligible)
- Query `Document` table scoped to `client_token`: match on `filename` + `file_size` + `content_hash`
- If found → reuse existing `session_id`, skip all parsing/chunking/embedding
- SHA256 protects against:
  - Same name + size but content changed (typo fix, metadata update)
  - Corrupted original upload
  - Two files coincidentally same name + size

**Rule 2 — Same filename + different size or different hash → Process fresh**
- Same name but content changed → must reprocess
- Insert new entry in `Document` table

**Rule 3 — Different filename + file < 2MB → Process fresh**
- Small file, dedup overhead not worth it
- Just process normally

**Rule 4 — Different filename + file ≥ 2MB → TF-IDF on 3 points**
- Extract first + middle + last 500 chars from newly parsed text
- Compare all 3 against stored chunks in `Document` table (scoped to `client_token`) using TF-IDF
- Dynamic threshold based on `avg_confidence`:
  - `avg_confidence < 0.85` → scanned/OCR doc → threshold = **0.90**
  - `avg_confidence ≥ 0.85` → native PDF → threshold = **0.95**
- All 3 points must exceed threshold → reuse `session_id`
- Any point below threshold → process fresh
- Checking 3 points (not just first+last) prevents false positives from boilerplate headers/footers

**Rule 5 — Document table unreachable → Skip dedup, process fresh**
- Entire dedup logic wrapped in `try/except`
- If DB down or ChromaDB fallback active → log warning → skip dedup → process normally
- Upload never crashes because of dedup failure

**Why TF-IDF not vector similarity:**
- No embedding API call → free + ~100ms
- Dedup needs word-level match, not semantic match
- Vector similarity is for retrieval — overkill and costly here

---

### Complete Decision Flow

```
Upload received + X-Client-Token
        ↓
Normalize filename to lowercase
        ↓
Compute SHA256 hash (~5ms)
        ↓
try:
    Query Document table (scoped to client_token, status="complete")
            ↓
    Same filename + same size + same hash?
        YES →  Verify vector collection still exists + has vectors
                    EXISTS  → ⚡ Reuse immediately (Rule 1)
                    MISSING → process fresh, update existing entry (Rule 1b)
        NO  ↓
    Same filename + different size or hash?
        YES → ⚙ Process fresh (Rule 2)
        NO  ↓
    File < 2MB?
        YES → ⚙ Process fresh (Rule 3)
        NO  ↓
    len(parsed_text) < 1500 chars?
        YES → ⚙ Process fresh — too short for reliable TF-IDF (Rule 4 edge case)
        NO  ↓
    Extract first + middle + last 500 chars
    TF-IDF against last 50 docs (scoped to client_token)
    avg_confidence < 0.85 → threshold=0.90 (OCR doc)
    avg_confidence ≥ 0.85 → threshold=0.95 (native PDF)
    All 3 scores > threshold →  Verify collection exists
                                    EXISTS  → ✅ Reuse (Rule 4a)
                                    MISSING → process fresh (Rule 4b)
    Any score ≤ threshold   → ⚙ Process fresh (Rule 4c)
except (DB down, any error):
    → ⚠ Log warning → skip dedup → process fresh (Rule 5)

--- After processing fresh ---
Insert Document entry with status="pending"
        ↓
Parse → Chunk → Embed → Write to vector store
        ↓
SUCCESS → set status="complete"
FAILURE → leave status="pending" (dedup will never reuse it)

--- Multi-file batch ---
Create ONE master session_id for the batch
For each file:
    cached → copy vectors into master session
    new    → write vectors into master session
Return master session_id to frontend
```

---

### Failure Cases & Fixes

**🔴 Critical — wrong answers or app breaks**

| Case | Problem | Fix |
|---|---|---|
| Vector store write fails after Document entry created | Dedup reuses session with no vectors → empty answers | `status` field — only reuse `status="complete"` entries. On failure → set `status="failed"`, show as failed in UI |
| Server restart clears ChromaDB but Document table keeps entry | Dedup reuses dead session_id → chat fails | Before reusing, verify collection exists + has vectors. If missing → process fresh → insert new entry |
| Partial upload / network drop | Partial embeddings → incomplete answers | Only set `status="complete"` after full vector store write. If fails → set `status="failed"` |
| Multi-file batch session confusion | 3 files → 3 different session_ids → chat only sees 1 file | ONE master session_id per batch. Copy cached vectors + write new vectors into master session |
| Confirmation gate — user walks away | SSE hangs forever, server holds open connection | 60s timeout on confirmation gate → auto-defaults to "process fresh" if no response |

**🟡 Medium — user frustrated**

| Case | Problem | Fix |
|---|---|---|
| Race condition — double click or two tabs | Both pass dedup → duplicate embeddings → weird answers | DB unique constraint on `(client_token, content_hash)`. First insert wins, second = cache hit |
| TF-IDF slow with 100+ uploads | Scans all docs → slow | Limit to last 50 docs per `client_token`. Index on `client_token` |
| File too short for 3-point check | Text < 1500 chars → first/middle/last overlap → unreliable | If `len(text) < 1500` → skip TF-IDF → use hash only |
| SSE can't resume after user clicks confirm | SSE is one-way, confirm comes via separate POST | `asyncio.Event()` — dedup holds event, `/upload/confirm` triggers it, SSE stream resumes |
| User clicks "Reprocess Fresh" | What happens to old entry? | Do nothing to old entry — just insert a new entry + process fresh. Old entry stays as historical record |
| Stale `status="pending"` entries | Failed uploads leave dead rows forever | Never delete — mark as `status="failed"`. Show failed uploads to user so they know |
| URL uploaded twice | Duplicate embeddings for same URL | Hash the URL string → same dedup rules as files (minus TF-IDF size check) |
| API endpoint called twice | Duplicate embeddings | Hash `url + headers` → same dedup rules |
| `/upload/confirm` spam | Anyone can spam endpoint | Tie confirm to a one-time token per upload — token expires after use or 60s |
| Multi-file batch mixed dedup | file1 needs confirm, file2 is new — do we pause all? | Show confirmations one at a time. Process new files in parallel while waiting for user decisions |

**🟢 Minor — inefficient but not harmful**

| Case | Problem | Fix |
|---|---|---|
| Incognito / localStorage cleared | New token → re-processes everything | Acceptable — just slower, answers still correct |
| Filename case sensitivity | `Report.pdf` vs `report.pdf` → misses | Normalize filename to lowercase before lookup |
| Unicode in filename | `résumé.pdf` vs `resume.pdf` → misses | Acceptable — just re-processes |
| SHA256 collision | Wrong reuse | Ignore — probability 1 in 2^256 |

---

### Logs at every step
```
[DEDUP] report.pdf (0.4MB) → hash match, reusing session=abc-123
[DEDUP] report.pdf (0.4MB) → hash mismatch, processing fresh
[DEDUP] invoice.pdf (1.2MB) → different filename, below 2MB threshold, processing fresh
[DEDUP] manual.pdf (5MB) → running TF-IDF (3-point), confidence=0.91, threshold=0.95
[DEDUP] manual.pdf (5MB) → scores=[0.97, 0.96, 0.98], reusing session=abc-123
[DEDUP] manual.pdf (5MB) → scores=[0.97, 0.61, 0.98], processing fresh
[DEDUP] DB unreachable → skipping dedup, processing fresh
```

### User Confirmation Gate

Before reusing ANY existing embeddings, ask the user first — safety net that catches all dedup failures.
Only shown when reusing. New files go straight through — no interruption.

**SSE event sent to frontend when match found:**
```json
{
  "type": "dedup_confirm",
  "filename": "report.pdf",
  "uploaded_ago": "2 hours ago",
  "file_size": "1.2 MB",
  "chunks_stored": 42,
  "reason": "same_hash" | "tfidf_match"
}
```

**Frontend shows confirmation card:**
```
┌─────────────────────────────────────────────────┐
│ ⚡ Existing embeddings found for report.pdf      │
│                                                  │
│  Uploaded: 2 hours ago                           │
│  Size: 1.2 MB · 42 chunks stored                │
│  Reason: identical file detected                 │
│                                                  │
│  [⚡ Use Existing]      [🔄 Reprocess Fresh]     │
└─────────────────────────────────────────────────┘
```

**User clicks "Use Existing"** → frontend sends `POST /upload/confirm` with `{ session_id, action: "reuse" }`
**User clicks "Reprocess Fresh"** → frontend sends `POST /upload/confirm` with `{ session_id, action: "reprocess" }`

**Why this is the ultimate safety net:**
- Catches ALL dedup wrong decisions before they affect the user
- User sees exactly when + why embeddings are being reused
- One click to override — zero frustration

---

### Performance & Code Quality Rules

**No duplicate code — single source of truth:**
- All dedup logic lives ONLY in `app/services/dedup.py` — routers never contain dedup logic
- All SSE event formatting lives ONLY in one `sse()` helper — never duplicated
- All vector store operations go through `get_vector_store()` — no direct ChromaDB/pgvector calls in routers

**Latency optimizations:**
- SHA256 computed in memory — no disk write (~5ms)
- DB query uses indexed columns only (`client_token`, `content_hash`) — sub-millisecond lookup
- TF-IDF runs only when needed (Rule 4) — not on every upload
- TF-IDF limited to last 50 docs — O(50) not O(n)
- Collection verification is a single COUNT query — not a full scan
- `status="pending"` insert uses `ON CONFLICT DO NOTHING` — race condition handled at DB level, no extra round trip
- All dedup steps run before any file parsing — fail fast before expensive operations

**Async everywhere:**
- All DB queries in dedup are `async` — never blocks FastAPI event loop
- TF-IDF computation runs in `asyncio.to_thread()` — CPU-bound, must not block event loop
- Vector copy for batch reuse runs async — doesn't delay SSE stream

---

### SSE Events (user sees in UI)

| Event | When |
|---|---|
| `dedup_confirm` | Match found — waiting for user decision |
| `⚙ New file detected — processing fresh` | No match found |
| `⚠ Dedup check skipped — processing fresh` | DB unreachable |

---

### File Upload Limits Per Conversation

| Limit | Value |
|---|---|
| Max files per session | 5 |
| Total size of all files combined | < 40MB |
| Reset | New chat → new session → limits reset completely |

**One rule — total combined size must be under 40MB regardless of how many files:**
```
User adds files (1 file or 5 files)
        ↓
Total combined size ≥ 40MB?
    YES → ❌ "Total upload size must be under 40MB. Start a new chat to upload more."
    NO  → ✅ Allow upload
```

**Enforced in both frontend and backend** — never trust frontend alone.

**UI shows running total as user adds files:**
```
3 files · 24.5 MB of 40 MB
```

**`.env` config:**
```env
MAX_FILES_PER_SESSION=5
MAX_SESSION_SIZE_MB=40
```

### Files to change

| File | What changes |
|---|---|
| `app/models/db.py` | Add all new fields + `uploaded_at`, `chunks_stored`, `status` to `Document` table. Unique constraint on `(client_token, content_hash)`. Index on `client_token` |
| `app/models/schemas.py` | Add `ConfirmRequest(session_id, action: "reuse" \| "reprocess", confirm_token)` and `ConfirmResponse` |
| `app/services/dedup.py` | New file — ALL dedup logic: hash, collection verify, TF-IDF 3-point, vector merge, one-time confirm token, asyncio.Event for SSE resume. Nothing else contains dedup logic |
| `app/routers/upload.py` | Read `X-Client-Token` header. Create master session_id per batch. Call `dedup.check()`. Pause SSE on `dedup_confirm`, resume on asyncio.Event. Add `POST /upload/confirm`. URL + API dedup |
| `app/static/chat.js` | Generate + store `client_token` in localStorage. Send as header. Render confirmation card on `dedup_confirm`. Auto-timeout card after 60s. Send confirm/reprocess. Show warning at 5 file limit. Show `status="failed"` entries |
| `alembic/versions/` | New migration for all new columns + constraints |
| `env.example` | Add `DEDUP_SIZE_THRESHOLD_MB=2`, update `MAX_FILES_PER_SESSION=5` |

### One Rule: No Logic in Routers
Routers only:
1. Read request → call service → stream SSE → return response
All business logic (dedup, parsing, chunking, embedding) stays in `app/services/`

---

## Phase 6 — Persistence + Speed 🔜 Next

### 6a — Supabase Free Tier (Persistent Sessions)
- Connect Supabase free PostgreSQL + pgvector
- Sessions survive server restarts
- Set `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` in HF Spaces secrets
- No code change needed — already supported

### 6b — Faster PDF Extraction
Options (in order of impact):
1. **Skip OCR for native PDFs** — try `ocr_enabled=False` first, fall back only for scanned docs (biggest speedup)
2. **Run parsing off event loop** — wrap `_parser.parse()` in `asyncio.to_thread()` (stops blocking FastAPI)
3. **Parallel pages** — pass `num_workers=N` to `LiteParse()` constructor

### 6c — Session History ✅ Done
- Per-session in-memory history (`_history_store` dict, threadsafe with `_history_lock`)
- Last 10 turns kept per session (20 messages total)
- Passed to intent check AND answer nodes — follow-up replies like "yup" work correctly
- Appended after every answer, cleared on session reset

---

## Phase 7 — LangGraph RAG Pipeline (Intent + HyDE + Query Rewriting + Neo4j) ✅ Done

### Problems Being Solved
1. **Small talk hits retrieval unnecessarily** — "hi", "how are you" triggers MMR search = wasted tokens
2. **Vague questions get bad context** — "what is in this doc" returns 3 random chunks → wrong/partial summary
3. **Mixed messages not handled** — "how are you, tell me the risks" needs routing, not just retrieval
4. **Vector search misses relationships** — "what happens if Acme breaches?" needs entity traversal, not chunk similarity

---

### Overall Flow

```
User Message
      ↓
Intent Check (always — fast Groq call, YES or NO, ~200ms)
"Does this need document retrieval?"
      ↓                         ↓
     NO                        YES
      ↓                         ↓
Direct LLM Answer        Advanced Mode ON?
(small talk,                ↓           ↓
 greetings)                YES          NO
                            ↓           ↓
                       LANGGRAPH    LANGGRAPH
                       ─────────    ─────────
                       Node 1:      Node 1:
                       Query        HyDE
                       Rewriting    → hypothetical
                       → 3 search     answer
                         variants   → 1 search
                            ↓         query
                       Node 2:          ↓
                       MMR × 3      Node 2:
                       + Neo4j      MMR × 1
                       Graph DB     (vector only)
                       traversal        ↓
                            ↓       Node 3:
                       Node 3:      LLM Answer
                       LLM Answer   (vector ctx)
                       (vector +
                        graph ctx)
```

---

### Intent Check (always on, outside graph)
```
Prompt: "Does this message ask about uploaded documents? Answer YES or NO only.
Message: {question}"
```
- Single token response — ~200ms, negligible tokens
- Handles mixed messages: "hi, tell me the risks" → YES
- NO → direct LLM answer, never enters LangGraph
- YES → check Advanced Mode toggle → enter correct LangGraph path

---

### Simple Path — Advanced Mode OFF

**Node 1 — HyDE**
```
Generate a hypothetical answer to the question.
Question: "what is in this NDA"
→ "This NDA covers confidentiality obligations between two parties,
   defines confidential information, sets a 2-year term..."
→ Use this hypothetical answer as the search query
```
- HyDE IS the query — no separate rewriting needed
- Hypothetical answer shape matches real chunk shape → better MMR results

**Node 2 — MMR Retrieval (vector only)**
- Run MMR with HyDE query → top k chunks
- Returns: chunks with source, page, chunk_index, confidence

**Node 3 — LLM Answer**
```
You are a helpful assistant analyzing uploaded documents.

Context:
{vector_context}

- If asked to summarize → summarize everything in the context
- If asked something specific → answer from context
- If asked for opinion/advice → reason from context and give your view
- If context has no relevant info → say so clearly

Question: {question}
```

---

### Advanced Path — Advanced Mode ON

**Node 1 — Query Rewriting**
```
Rewrite this question into 3 different search queries.
Question: "what are the risks in this NDA"
→ "NDA risk clauses liability obligations"
→ "confidentiality agreement potential issues penalties"
→ "legal risks unlimited liability breach consequences"
```
- 3 variants cover different angles of the same question
- No HyDE here — graph DB handles the relationship/entity side

**Node 2 — Parallel Retrieval (Vector + Neo4j Graph)**

*Vector side:*
- Run MMR for each of 3 query variants independently
- Merge all results, deduplicate by chunk content
- Returns: text chunks with source, page, chunk_index, confidence

*Neo4j Graph side:*
- Built during ingestion — entities + relationships extracted from chunks
- At query time: traverse graph for entities mentioned in question
```
"Acme" → Party node
       → obligated-to → [protect Confidential Information]
       → breach → Consequence node → [penalty, termination]
```
- Returns: entity paths + related facts as structured text

*Merge:*
- Combine vector chunks + graph facts into single context
- Deduplicate overlapping content

**Node 3 — LLM Answer**
```
You are a helpful assistant analyzing uploaded documents.

Context (from documents):
{vector_context}

Related entities and relationships:
{graph_context}

- If asked to summarize → summarize everything in the context
- If asked something specific → answer from context
- If asked for opinion/advice → reason from context and give your view
- If context has no relevant info → say so clearly

Question: {question}
```

---

### Neo4j Graph DB — Ingestion Side
During file upload, after chunking, run entity extraction per chunk:
```
Text chunk → LLM extracts:
  Entities: [Acme Corp, Beta Inc, Confidential Information, 2 years]
  Relations: [Acme Corp]  --signs-->         [NDA]
             [Acme Corp]  --obligated-to-->   [protect Confidential Information]
             [Beta Inc]   --receives-->        [Confidential Information]
             [NDA]        --expires-->         [2 years]
             [Breach]     --results-in-->      [Termination + Penalty]
```
Store in **Neo4j AuraDB free tier** — persistent, survives server restarts.

---

### What Each Question Gets
| Question | Intent | Mode | Path | Result |
|---|---|---|---|---|
| "hi" | NO | any | Direct LLM | Friendly reply, no retrieval |
| "what is in this doc" | YES | OFF | HyDE → MMR | Hypothetical summary → finds broad chunks → summarizes |
| "what are the risks" | YES | OFF | HyDE → MMR | Hypothetical risk answer → finds risk chunks |
| "what happens if Acme breaches" | YES | ON | Rewrite → MMR + Neo4j | Graph: Acme→breach→consequence + chunks |
| "how are section 3 and 7 related" | YES | ON | Rewrite → MMR + Neo4j | Graph traversal finds shared entity nodes |
| "should I sign this" | YES | ON | Rewrite → MMR + Neo4j | Key clause chunks + obligation graph → LLM advises |

---

### Toggle Button (UI)
- Toggle in chat header — **"Advanced Mode"** ON/OFF
- Default: OFF
- State saved in `localStorage`
- Sent as `X-Advanced-Mode: true/false` header with every chat request

---

### Why LangGraph for Both Paths
- **State machine** — each node has one job, easy to debug
- **Conditional edges** — routing between simple/advanced is explicit
- **Extensible** — Map-Reduce, hybrid search, session memory = just add a node
- **LangChain native** — works directly with ChromaDB/pgvector retrievers

---

### Files to Change
| File | What changes |
|---|---|
| `app/services/rag.py` | Replace `answer_question()` with two LangGraph pipelines: `simple_pipeline()` (HyDE+MMR) and `advanced_pipeline()` (QueryRewrite+MMR+Neo4j). Intent check before both. |
| `app/services/graph_store.py` | New file — Neo4j driver, `build_graph(chunks, session_id)`, `query_graph(question, session_id)` |
| `app/routers/upload.py` | After chunking, call `graph_store.build_graph()` async (doesn't block SSE) |
| `app/routers/chat.py` | Read `X-Advanced-Mode` header → call simple or advanced pipeline |
| `app/static/chat.js` | Add Advanced Mode toggle button, save to localStorage, send as header |
| `app/static/style.css` | Toggle button styles |
| `pyproject.toml` | Add `langgraph`, `neo4j` dependencies |
| `app/config.py` | Add `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `RETRIEVAL_K` |
| `env.example` | Add Neo4j credentials |

### Config
```env
NEO4J_URI=neo4j+s://xxxxx.databases.neo4j.io   # AuraDB free tier URI
NEO4J_USER=neo4j
NEO4J_PASSWORD=your-password
RETRIEVAL_K=3          # chunks returned per MMR query
```

### Failure Cases + Fallback Strategy

**Golden Rule: If anything fails in Advanced Mode → fall back to Simple Mode silently, log the error, show message to user.**

```
Advanced Mode pipeline
        ↓
  Any node fails?
        ↓
       YES
        ↓
Log: [ADVANCED] {node} failed: {error} — falling back to simple pipeline
        ↓
Show user: "Advanced mode unavailable, using standard mode"
        ↓
Run Simple Mode pipeline instead
        ↓
Return answer normally
```

**Specific failure cases:**

| Failure | Where | Fallback | User Message | Log |
|---|---|---|---|---|
| Intent check fails (Groq down) | Before graph | Default to YES → simple pipeline | none — transparent | `[INTENT] check failed, defaulting to retrieval` |
| Query rewriting fails | Advanced Node 1 | Fall back to simple pipeline (HyDE) | "Advanced mode unavailable, using standard mode" | `[ADVANCED] query rewrite failed: {error}` |
| Neo4j connection down | Advanced Node 2 | Skip graph, use vector only, continue | "Advanced mode unavailable, using standard mode" | `[ADVANCED] Neo4j unavailable: {error}` |
| Neo4j free tier limit hit | Advanced Node 2 | Skip graph, use vector only, continue | "Advanced mode unavailable, using standard mode" | `[ADVANCED] Neo4j limit reached: {error}` |
| BM25 index missing (restart) | Node 2 both paths | Rebuild from ChromaDB chunks on demand | none — transparent | `[BM25] index missing, rebuilding for session={session_id}` |
| Reranker API down | Node 2 both paths | Skip reranking, use RRF top 5 directly | none — transparent | `[RERANK] BGE reranker unavailable, using RRF top 5` |
| Entity extraction fails at ingestion | Upload | Skip graph build, log warning, upload completes | none — upload still works | `[GRAPH] entity extraction failed for {filename}: {error}` |
| Neo4j session mismatch (reuse) | Advanced Node 2 | Copy entities from old session to new session | none — transparent | `[GRAPH] copying entities session={old} → session={new}` |
| Map-Reduce hits Groq rate limit | Phase 7d node | Stop at current batch, summarize what's done | "Partial summary — rate limit reached, try again" | `[MAPREDUCE] rate limit at batch {n}/{total}` |
| HyDE generates wrong query | Simple Node 1 | Use original question as fallback query too | none — transparent | `[HYDE] using original question as fallback query` |

**UI message shown to user (non-blocking toast):**
```
⚠ Advanced mode unavailable — using standard mode
```
Never blocks the answer — user still gets a response, just via simple pipeline.

---

### Negatives to Keep in Mind
| Risk | Mitigation |
|---|---|
| Intent check adds ~200ms always | 1 token call — acceptable, user won't notice |
| Advanced mode: 2 extra LLM calls (rewrite + entity extract at ingest) | Rewrite = ~100 tokens. Entity extract runs at upload time, not query time |
| Neo4j AuraDB free tier — 1 instance only | Fine for dev/demo. Paid tier for production |
| Entity extraction at ingestion adds time | Run async after chunking — doesn't block SSE stream |
| Graph query returns irrelevant entities | Filter by session_id — only entities from uploaded docs |

---

### Phase 7a — Core Pipeline ✅ Done
- Intent check always on — fast Groq model, YES/NO only
- Simple LangGraph path: HyDE (run in parallel with intent) → BM25+MMR+RRF+Rerank → LLM answer
- Advanced Mode toggle in UI (saved to localStorage, sent as `X-Advanced-Mode` header)

### Phase 7b — Advanced Pipeline ✅ Done
- Advanced LangGraph path: Query Rewriting → MMR×3 + Neo4j → merged LLM answer
- Neo4j AuraDB entity extraction at ingestion time (async, doesn't block SSE)
- On any advanced node failure → silent fallback to simple pipeline

### Phase 7c — Hybrid Search + Reranking ✅ Done
Full pipeline for Node 2 (both simple and advanced paths):
```
BM25 (top 7) + Vector MMR (top 7)
      ↓               ↓
      └──── RRF merge (top 7) ────┘
                  ↓
      BAAI/bge-reranker-large
      (HuggingFace Inference API, free tier)
      scores each (question, chunk) pair
                  ↓
              top 5 chunks
                  ↓
            LLM Answer
```
- **BM25** — exact keyword match (clause numbers, dates, amounts, names)
- **Vector MMR** — semantic similarity (meaning-based retrieval)
- **RRF** — merges both by rank position, not score (scale-independent)
- **BGE Reranker** — cross-encoder scores each chunk against question, picks best 5

Config (in `config.yaml`):
```yaml
retrieval:
  hybrid_top_k: 7
  rerank_top_n: 5
  reranker_model: BAAI/bge-reranker-large
```

### Phase 7d — Map-Reduce for Large Docs 📋 Planned
- New LangGraph node: chunks → batches of 10 → LLM summarizes each → combine → final answer
- Auto-triggered when chunk count exceeds threshold (e.g. 50 chunks)

### Phase 7e — Session Memory ✅ Done
- Per-session `_history_store` dict, threadsafe with `threading.Lock()`
- Last 10 turns (20 messages) passed to intent check and every answer node
- "yup", "explain that", "what else?" all resolve correctly using history
- Stored in `app/services/rag.py` — in-memory per server process

---

## Phase 8 — Performance Optimisations ✅ Done

### 8a — Groq Fallback Chain
- Primary: `llama-3.1-8b-instant` (fast, 500K tokens/day)
- Fallback chain: `llama-3.3-70b-versatile` → `llama3-8b-8192` → `gemma2-9b-it` → `llama3-70b-8192`
- Last resort: OpenAI `gpt-4o-mini` (if `openai_fallback_enabled: true` in config.yaml)
- All models use `max_retries=0` — no 25s SDK waits; our fallback code runs immediately
- Full chain configurable via `config.yaml`, no code changes needed

### 8b — Embedding Batch Optimisation
- Old: one `add_texts([chunk])` call per chunk — 40 chunks × 1.3s = 52s
- New: `_add_texts_batched()` — all chunks in batches of 32 → 7.5× faster
- `EMBED_BATCH_SIZE = 32` constant prevents HF API payload/timeout errors

### 8c — Response Latency (16s → 8-10s)
- **Parallel intent + HyDE** — `ThreadPoolExecutor` runs both simultaneously; HyDE cancelled immediately if intent=NO
- **Fast intent model** — dedicated `llama-3.1-8b-instant` for YES/NO (separate from answer model)
- **Advanced drops HyDE** — advanced mode starts at query-rewrite node, skips HyDE entirely
- **Answer cache** — MD5-keyed in-memory cache (256 entries, scoped per session+question+mode)

---

## Phase 9 — Future Enhancements 📋 Planned

- **More file types** — CSV, Excel, Markdown, PowerPoint
- **WhatsApp integration** — already started (`app/routers/whatsapp.py`) — needs Twilio webhook setup

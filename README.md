---
title: RAG Chat With Data
emoji: 📄
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 8001
pinned: false
---

# datascout-ai — RAG Data Assistant

A production-ready RAG (Retrieval-Augmented Generation) backend that lets users chat with their data. Upload files, scrape URLs, or pull from JSON APIs — the backend parses, chunks, embeds, and stores everything in pgvector, then answers questions using an LLM with MMR retrieval.

---

## Tech Stack

| Layer | Technology |
|---|---|
| **API Server** | FastAPI + uvicorn (port 8011) |
| **Runtime** | Python 3.13 + uv (no pip, no requirements.txt) |
| **Database** | Supabase — PostgreSQL 17.6 + pgvector 0.8.0 |
| **Vector Store** | pgvector (primary) → ChromaDB (automatic fallback) |
| **Embeddings** | BAAI/bge-large-en-v1.5 via HuggingFace Inference API (primary) → OpenAI text-embedding-3-small (fallback) |
| **LLM** | OpenAI gpt-4o-mini |
| **Document Parsing** | LiteParse v2 (PDF, DOC, DOCX) + plain decode (TXT) |
| **URL Scraping** | Crawl4AI (primary) → Playwright Chromium (fallback) |
| **API Ingestion** | httpx (async JSON fetch with optional headers) |
| **Retrieval** | MMR — Maximum Marginal Relevance |
| **Migrations** | Alembic |
| **Containerisation** | Docker (Python 3.13-slim + uv) |
| **Logging** | Rotating file logs — `logs/app.log` (10 MB x 5 files) |

---

## Quickstart

**Requirements:** Python 3.13, [uv](https://docs.astral.sh/uv/getting-started/installation/)

```bash
# 1. Clone
git clone https://github.com/your-org/datascout-ai.git
cd datascout-ai

# 2. Install all dependencies (one command, no pip needed)
uv sync

# 3. Download Playwright Chromium browser (one time only)
uv run setup

# 4. Copy env template and fill in your keys
cp .env.example .env

# 5. Run database migrations
uv run alembic upgrade head

# 6. Start the server
uv run uvicorn app.main:app --host 0.0.0.0 --port 8011 --reload
```

Server runs at `http://localhost:8011`

> **Note:** Step 3 downloads ~175 MB of browser binaries to `~/.cache/ms-playwright/`. Only needed once per machine. The server also auto-checks and installs Playwright browsers on every boot via the FastAPI lifespan event — so new team members never hit a missing browser error.

---

## Environment Variables

Copy `.env.example` to `.env` and fill in your values. All credentials are split into individual fields — never a single long connection URL.

```env
# ── OpenAI ──────────────────────────────────────────────────────
OPENAI_API_KEY=sk-proj-...

# ── Supabase Database ───────────────────────────────────────────
# Get from: Supabase dashboard → Project Settings → Database
DB_HOST=db.<your-ref>.supabase.co
DB_PORT=5432
DB_USER=postgres
DB_PASSWORD=your-password
DB_NAME=postgres

# ── Supabase API ────────────────────────────────────────────────
SUPABASE_URL=https://<your-ref>.supabase.co
SUPABASE_ANON_KEY=eyJ...

# ── App ─────────────────────────────────────────────────────────
APP_ENV=development
APP_PORT=8011

# ── Embeddings ──────────────────────────────────────────────────
# Change EMBEDDING_PROVIDER to switch priority — no code change needed
#   bge    → BAAI/bge-large-en-v1.5 via HuggingFace Inference API (1024 dims, free tier)
#   openai → text-embedding-3-small via OpenAI (1536 dims, paid)
EMBEDDING_PROVIDER=bge
HUGGINGFACE_API_KEY=hf_...
HUGGINGFACE_MODEL_ID=BAAI/bge-large-en-v1.5
EMBEDDING_DIM=1024

# ── Retrieval (MMR) ─────────────────────────────────────────────
RETRIEVAL_K=3
RETRIEVAL_FETCH_K=10
RETRIEVAL_LAMBDA=0.7

# ── Ingestion Limits ────────────────────────────────────────────
MAX_FILE_SIZE_MB=15
MAX_FILES_PER_SESSION=3
```

---

## RAG Pipeline — 4 Steps

```
1. INGESTION      →    2. STORING         →    3. RETRIEVAL        →    4. LLM ANSWER
   Files / URLs             pgvector                MMR search                gpt-4o-mini
   / JSON APIs              (Supabase)              diverse, non-             generates answer
   parsed &                 ChromaDB                redundant chunks          from retrieved
   chunked                  fallback                                          context
```

---

## Step 1 — Ingestion

### File Upload

- **Supported formats:** PDF, DOC, DOCX, TXT
- **Max file size:** controlled by `MAX_FILE_SIZE_MB` in `.env` (default 15 MB)
- **Max files per session:** controlled by `MAX_FILES_PER_SESSION` in `.env` (default 3)
- All files upload in parallel — not one at a time

**Parsing strategy:**

| Format | Parser | Notes |
|---|---|---|
| `.txt` | Plain `decode("utf-8")` | No external parser needed |
| `.pdf` `.doc` `.docx` | LiteParse v2 | Rust-based, ~100x faster than PyPDF2/python-docx, no API key |

LiteParse v2 is made by LlamaIndex, fully open source, runs completely locally.

### URL Scraping — Crawl4AI + Playwright Fallback

```
User pastes URL
        |
        v
Try Crawl4AI (primary)
        |                           |
     success                     fails
        |                           |
Use extracted markdown      Try Playwright Chromium (fallback)
                                    |                   |
                                 success              fails
                                    |                   |
                            Use extracted text    Return error to user
```

- **Crawl4AI** handles both plain HTML and JavaScript-rendered pages (React, Vue, Angular) — built specifically for AI/RAG use cases
- **Playwright** (Chromium headless) is the automatic fallback — used if Crawl4AI returns empty content or throws an error
- Playwright browsers are installed automatically on server boot via the FastAPI lifespan event — no manual step needed after the first `uv run setup`

### JSON API Ingestion

- User provides an API URL that returns JSON data
- **Headers are optional** — only needed if the API requires authentication (e.g. `Authorization: Bearer <token>`)
- Fetched asynchronously with `httpx` (30s timeout)
- JSON is recursively flattened into plain text, then goes through the same RAG pipeline as files

---

## Step 2 — Vector Store

### pgvector (Primary)

Vectors are stored in Supabase (PostgreSQL + pgvector extension). Persistent, cloud-hosted, survives restarts. Uses `langchain-postgres` with the `langchain_pg_embedding` table managed automatically.

### ChromaDB (Automatic Fallback)

```
Try connect to Supabase pgvector
        |                                   |
     success                             fails
        |                                   |
Use pgvector (cloud)              Use ChromaDB (local disk — ./chroma_data/)
                                  Logs WARNING so you know
                                  Data persists across restarts
```

ChromaDB is chosen as fallback over FAISS because ChromaDB persists to disk — FAISS is in-memory only and loses all data on every restart.

---

## Step 3 — Retrieval (MMR)

MMR (Maximum Marginal Relevance) picks chunks that are both **relevant to the question** AND **different from each other**. Without MMR, top-3 results could be 3 near-identical paragraphs — wasting context window space.

All tunable via `.env` — no code changes needed:

| Variable | Default | Meaning |
|---|---|---|
| `RETRIEVAL_K` | `3` | Chunks sent to the LLM (more = richer context, slower + costlier) |
| `RETRIEVAL_FETCH_K` | `10` | Candidate pool MMR selects from (must be >= RETRIEVAL_K) |
| `RETRIEVAL_LAMBDA` | `0.7` | `1.0` = pure similarity · `0.0` = pure diversity · `0.7` = balanced |

---

## Embedding Strategy

BGE runs via the **HuggingFace Inference API** — no model downloaded locally or on the server. Called over HTTP using a token. Nothing stored on disk.

```
EMBEDDING_PROVIDER=bge    → Try BGE first → fallback to OpenAI if BGE fails
EMBEDDING_PROVIDER=openai → Try OpenAI first → fallback to BGE if OpenAI fails
```

Switch provider by changing one line in `.env` — no code changes needed.

| Provider | Model | Dimensions | Cost |
|---|---|---|---|
| **BGE** (default) | `BAAI/bge-large-en-v1.5` | 1024 | Free tier available |
| **OpenAI** (fallback) | `text-embedding-3-small` | 1536 | Paid |

> If you switch provider, update `EMBEDDING_DIM` to match and run an Alembic migration — the vector column dimension must change. Then re-process all documents (old embeddings are in the wrong dimensional space).

```bash
alembic revision --autogenerate -m "update embedding dimension"
alembic upgrade head
```

---

## Database Migrations (Alembic)

Alembic manages the database schema. Define tables in Python (SQLAlchemy models) — Alembic creates and updates them in Supabase automatically. No manual SQL needed.

**Tables:**

| Table | Purpose |
|---|---|
| `sessions` | Chat sessions — `id`, `created_at` |
| `documents` | Uploaded files/URLs — `id`, `session_id`, `filename`, `file_type`, `uploaded_at` |
| `langchain_pg_embedding` | Vector embeddings — managed by langchain-postgres |
| `chat_history` | Message history — `id`, `session_id`, `role`, `message`, `created_at` |

**Commands:**

```bash
# First time — create all tables
uv run alembic upgrade head

# After changing a SQLAlchemy model
uv run alembic revision --autogenerate -m "describe your change"
uv run alembic upgrade head

# Check current migration state
uv run alembic current
```

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check — `{"status": "ok"}` |
| `POST` | `/upload/files` | Upload PDF/DOC/DOCX/TXT files — streams progress via SSE |
| `POST` | `/upload/url` | Scrape a URL (Crawl4AI + Playwright fallback) — streams progress via SSE |
| `POST` | `/upload/api` | Fetch a JSON API endpoint with optional headers |
| `POST` | `/chat/` | Ask a question — `{session_id, question}` → `{session_id, answer}` |

`/upload/files` and `/upload/url` return **Server-Sent Events (SSE)** — the client receives real-time progress events as each step completes (parse → chunk → embed → store), including elapsed time per step.

---

## Project Structure

```
app/
├── main.py                   # FastAPI app, lifespan (Playwright auto-install), router mounts
├── core/
│   └── logging_config.py     # Rotating file + console logger
├── routers/
│   ├── upload.py             # /upload/files, /upload/url, /upload/api (SSE streaming)
│   └── chat.py               # /chat/
├── services/
│   ├── ingestion.py          # LiteParse, Crawl4AI, Playwright fallback, httpx API fetch
│   ├── vector_store.py       # pgvector primary, ChromaDB fallback, embedding provider selection
│   └── rag.py                # MMR retriever, LCEL chain, gpt-4o-mini
├── models/
│   ├── db.py                 # SQLAlchemy table definitions (dynamic EMBEDDING_DIM from .env)
│   └── schemas.py            # Pydantic request/response models
├── db/
│   └── session.py            # SQLAlchemy engine (urllib.parse.quote_plus for special chars in password)
└── static/                   # Frontend served by FastAPI (HTML/CSS/JS)

alembic/                      # Migration scripts
alembic.ini                   # Alembic config
planning/                     # PLAN.md, PLAN-backend.md, PLAN-frontend.md
pyproject.toml                # Dependencies + uv scripts
Dockerfile                    # Python 3.13-slim + uv + Playwright
logs/                         # Rotating log files (gitignored)
chroma_data/                  # ChromaDB local fallback data (gitignored)
.env                          # Credentials (gitignored — never committed)
```

---

## Logging

Every step from upload to answer is logged to both terminal and `logs/app.log`:

```
2026-06-08 10:23:41 | INFO     | app.main                  | Server starting — checking Playwright...
2026-06-08 10:23:41 | INFO     | app.main                  | Playwright ready. Server is up.
2026-06-08 10:23:55 | INFO     | app.routers.upload        | [UPLOAD] session=abc-123, files=1
2026-06-08 10:23:55 | INFO     | app.services.ingestion    | [PARSE] Starting — file=report.pdf, size=142.3 KB
2026-06-08 10:23:55 | INFO     | app.services.ingestion    | [PARSE] Done (LiteParse) — chars=18420, time=0.43s
2026-06-08 10:23:55 | INFO     | app.routers.upload        | [CHUNK] report.pdf → 22 chunks
2026-06-08 10:23:55 | INFO     | app.services.vector_store | [EMBED] Using BAAI/bge-large-en-v1.5 (HuggingFace Inference API)
2026-06-08 10:23:55 | INFO     | app.services.vector_store | [STORE] Connected to pgvector (Supabase)
2026-06-08 10:23:57 | INFO     | app.routers.upload        | [UPLOAD] Complete — 1 file(s), 22 chunks, time=2.14s
2026-06-08 10:24:10 | INFO     | app.services.rag          | [RETRIEVE] Question: 'What is the main topic?'
2026-06-08 10:24:10 | INFO     | app.services.rag          | [LLM] Calling gpt-4o-mini...
2026-06-08 10:24:12 | INFO     | app.services.rag          | [LLM] Done — answer_length=312 chars, total_time=1.87s
```

Log files rotate at 10 MB, keeping the last 5 files. The `logs/` folder is created automatically on first run — works identically locally and on AWS.

---

## Docker

```bash
# Build
docker build -t datascout-ai .

# Run
docker run -p 8011:8011 --env-file .env datascout-ai
```

The Dockerfile:
- Uses Python 3.13-slim + uv with layer caching for fast rebuilds
- Installs Playwright Chromium at build time with `--with-deps` (includes system dependencies)
- The image is fully self-contained — no extra setup needed on any machine or AWS instance

---

## Supabase Setup (First Time)

1. Create a free project at [supabase.com](https://supabase.com)
2. Go to **SQL Editor** and enable the pgvector extension:
   ```sql
   CREATE EXTENSION IF NOT EXISTS vector;
   ```
3. Copy credentials from **Project Settings → Database** into your `.env`
4. Run migrations to create all tables:
   ```bash
   uv run alembic upgrade head
   ```

Free tier: 500 MB storage · 5 GB bandwidth/month · pauses after 1 week of inactivity (unpause manually from dashboard).

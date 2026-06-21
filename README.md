---
title: RAG Chat With Data
emoji: 📄
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 8001
pinned: false
---

# RAG Data Assistant

Chat with your data. Upload PDFs, scrape URLs, or pull from JSON APIs — then ask questions and get AI answers grounded in your documents, with cited sources and page numbers.

---

## Tech Stack

| Layer | Technology |
|---|---|
| **API Server** | FastAPI + uvicorn |
| **Runtime** | Python 3.13 + uv |
| **LLM** | Groq `llama-3.1-8b-instant` (primary, free) → 4 Groq fallback models → OpenAI `gpt-4o-mini` (last resort) |
| **RAG Orchestration** | LangGraph state machine |
| **Hybrid Search** | BM25 (keyword) + MMR (semantic) → RRF merge → BGE Reranker |
| **Graph DB** | Neo4j AuraDB — entity & relationship traversal (Advanced Mode) |
| **Vector Store** | ChromaDB local (default) → pgvector on Supabase (optional) |
| **Embeddings** | `BAAI/bge-large-en-v1.5` via HuggingFace Inference API (free) → OpenAI fallback |
| **Document Parsing** | LiteParse v2 — Rust-based, page-level metadata |
| **URL Scraping** | Crawl4AI → Playwright Chromium fallback |
| **Container** | Docker — Python 3.13-slim, non-root user |
| **Deployment** | HuggingFace Spaces (free) |
| **Migrations** | Alembic |

---

## How It Works

### Step 1 — Upload (Ingestion)

When you upload a file, paste a URL, or provide an API endpoint, the system:

1. **Parses** the content — LiteParse v2 extracts text with page numbers and OCR confidence scores per page
2. **Chunks** the text — splits into overlapping chunks (~1000 chars, 200 overlap) so no context is lost at boundaries
3. **Embeds** the chunks — converts each chunk to a vector using `BAAI/bge-large-en-v1.5` (batched in groups of 32 for speed)
4. **Stores** vectors in ChromaDB (or pgvector) — scoped to your session ID so your data stays private
5. **Builds a knowledge graph** — extracts entities and relationships from each chunk and stores them in Neo4j (used in Advanced Mode)

Every step streams live progress to the UI via Server-Sent Events.

---

### Step 2 — Ask a Question (RAG Pipeline)

When you send a message, the system runs a LangGraph state machine. There are two modes:

#### Standard Mode

```
Your message
      |
      v
[Intent Check]
Is this a question about the documents, or just small talk?
Uses a fast LLM call (YES / NO only).
      |
      +-- NO --> Direct LLM reply (greetings, general chat — no retrieval needed)
      |
      +-- YES
            |
            v
      [HyDE — Hypothetical Document Embedding]
      The LLM generates a hypothetical answer to your question.
      That hypothetical answer is used as the search query —
      it matches the shape and vocabulary of real document chunks
      far better than the raw question does.
      (Runs in parallel with the intent check to save time.)
            |
            v
      [Hybrid Retrieval]
      BM25 (keyword search) — finds exact matches: clause numbers,
        dates, amounts, names
      +
      MMR (vector search) — finds semantically similar chunks
        even if the wording differs
      Both return top 7 candidates each.
            |
            v
      [RRF — Reciprocal Rank Fusion]
      Merges BM25 and MMR results by rank position, not score.
      No normalisation needed. Produces a single ranked list.
            |
            v
      [BGE Reranker — BAAI/bge-reranker-large]
      Cross-encoder: scores every (question, chunk) pair directly.
      Picks the top 5 chunks that best answer your specific question.
            |
            v
      [LLM Answer]
      Groq LLM generates the final answer using only those 5 chunks
      as context. Response includes citations: source file, page
      number, and OCR confidence.
```

#### Advanced Mode (toggle in the UI)

Advanced Mode replaces HyDE with Query Rewriting and adds Neo4j graph traversal on top of vector search. Best for complex questions that involve relationships between entities.

```
Your message
      |
      v
[Intent Check] — same as above
      |
      +-- YES
            |
            v
      [Query Rewriting]
      The LLM rewrites your question into 3 different search queries,
      each covering a different angle of the same topic.
      Example: "what are the risks in this NDA"
        -> "NDA risk clauses liability obligations"
        -> "confidentiality agreement potential issues penalties"
        -> "legal risks unlimited liability breach consequences"
            |
            v
      [Hybrid Retrieval x 3]
      BM25 + MMR run for all 3 query variants.
      Results are merged and deduplicated.
            |
            +----------------------------+
            |                            |
            v                            v
      [Vector chunks]           [Neo4j Graph Traversal]
      Same RRF + Reranker       Entities from your question
      pipeline as Standard      (e.g. "Acme", "breach") are
      Mode.                     looked up in the knowledge
                                graph built at upload time.
                                Returns related facts:
                                Acme -> obligated-to -> protect data
                                breach -> results-in -> penalty
            |                            |
            +----------------------------+
                          |
                          v
                    [LLM Answer]
                    Gets both vector chunks AND graph facts
                    as context. Produces a richer, more
                    accurate answer for relationship questions.
                          |
                    (any step fails)
                          |
                          v
                    Silent fallback to Standard Mode
```

---

### Conversation History

The system keeps the last 10 turns of your conversation in memory per session. This means follow-up replies work exactly as expected:

```
You:       "Want me to explain clause 3 in detail?"
Assistant: "Sure! Clause 3 says..."
You:       "yup"
```

On "yup", the intent check sees the full conversation history, understands it refers to the previous document question, retrieves clause 3, and answers — rather than treating "yup" as a standalone message.

---

### LLM Fallback Chain

If the primary Groq model hits its rate limit, the system automatically tries the next model with no delay:

```
llama-3.1-8b-instant     <-- primary (fast, 500K tokens/day free)
      | rate limited
llama-3.3-70b-versatile  <-- higher quality, 100K tokens/day
      | rate limited
llama3-8b-8192
      | rate limited
gemma2-9b-it
      | rate limited
llama3-70b-8192
      | all exhausted
OpenAI gpt-4o-mini       <-- last resort (requires OPENAI_API_KEY)
```

All models use `max_retries=0` so there are no 25-second SDK waits — our own fallback code kicks in immediately. The full chain is configurable in `config.yaml`.

---

### Answer Cache

Identical questions (same session, same question text, same mode) are served from an in-memory cache. No LLM call, no retrieval — instant response. Cache holds 256 entries, keyed by MD5 of `session_id + question + mode`.

---

## Quickstart

**Requirements:** Python 3.13, [uv](https://docs.astral.sh/uv/getting-started/installation/)

```bash
# 1. Clone
git clone https://github.com/your-org/rag-data-assistant.git
cd rag-data-assistant

# 2. Install dependencies
uv sync

# 3. Download Playwright browser (one time only)
uv run setup

# 4. Copy env template and fill in your keys
cp env.example .env

# 5. Run database migrations
uv run alembic upgrade head

# 6. Start the server
uv run uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
```

Open `http://localhost:8001` in your browser.

---

## Environment Variables

Copy `env.example` to `.env` and fill in your values.

```env
# Groq — primary LLM (free)
GROQ_API_KEY=gsk_...

# OpenAI — fallback LLM (optional)
OPENAI_API_KEY=sk-...

# HuggingFace — embeddings (free)
HUGGINGFACE_API_KEY=hf_...

# Supabase pgvector — optional, falls back to ChromaDB if not set
DB_HOST=db.<your-ref>.supabase.co
DB_PORT=5432
DB_USER=postgres
DB_PASSWORD=your-password
DB_NAME=postgres

# Neo4j AuraDB — optional, only needed for Advanced Mode
NEO4J_URI=neo4j+s://xxxxx.databases.neo4j.io
NEO4J_USER=neo4j
NEO4J_PASSWORD=your-password

APP_ENV=development
```

Model names, retrieval tuning, and upload limits are all in `config.yaml` — edit there, no code changes needed.

---

## Features

- **File upload** — PDF, DOC, DOCX, TXT. Drag & drop supported. Up to 5 files, 50 MB total per session.
- **URL scraping** — paste any URL and chat with the page content. Handles JS-rendered pages.
- **JSON API ingestion** — point at any API endpoint, add auth headers if needed.
- **Cited answers** — every answer shows source file, page number, and OCR confidence.
- **Advanced Mode** — toggle in the chat UI for deeper answers using graph traversal.
- **Multi-turn chat** — follow-up messages use full conversation context.
- **Real-time upload progress** — each step (parse → chunk → embed → store) streams live.
- **Automatic fallbacks** — LLM, embeddings, and vector store all have fallback providers.

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check — `{"status": "ok"}` |
| `POST` | `/upload/files` | Upload PDF/DOC/DOCX/TXT — streams SSE progress |
| `POST` | `/upload/url` | Scrape a URL — streams SSE progress |
| `POST` | `/upload/api` | Fetch a JSON API with optional auth headers |
| `POST` | `/chat/` | Ask a question — returns answer + citations + elapsed_ms |

**Chat headers:**
- `X-Advanced-Mode: true` — enables Advanced Mode pipeline

---

## Project Structure

```
app/
├── main.py              # FastAPI app, lifespan
├── config.py            # Loads config.yaml + .env
├── routers/
│   ├── upload.py        # Upload endpoints (SSE streaming)
│   └── chat.py          # Chat endpoint
├── services/
│   ├── rag.py           # LangGraph pipelines, intent, history, cache, fallbacks
│   ├── ingestion.py     # LiteParse, Crawl4AI, Playwright, httpx
│   ├── vector_store.py  # Embedding + vector store with fallback
│   └── dedup.py         # Duplicate file detection
├── models/
│   ├── db.py            # SQLAlchemy models
│   └── schemas.py       # Pydantic schemas
├── db/
│   └── session.py       # Database engine
└── static/              # Frontend (HTML/CSS/JS, served by FastAPI)

alembic/                 # Migration scripts
config.yaml              # Model names, retrieval params, limits
tests/                   # pytest suite (31 tests)
pyproject.toml           # Dependencies + uv scripts
Dockerfile
```

---

## Running Tests

```bash
uv run pytest tests/ -v
```

---

## Docker

```bash
docker build -t rag-assistant .
docker run -p 8001:8001 --env-file .env rag-assistant
```

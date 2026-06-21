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
| **LLM** | Groq (primary, free) → OpenAI fallback |
| **Vector Store** | ChromaDB (local) → pgvector on Supabase (optional) |
| **Embeddings** | BAAI/bge-large-en-v1.5 via HuggingFace API (free) → OpenAI fallback |
| **Document Parsing** | LiteParse v2 — PDF, DOC, DOCX, TXT |
| **URL Scraping** | Crawl4AI → Playwright fallback |
| **Container** | Docker — Python 3.13-slim |
| **Deployment** | HuggingFace Spaces (free) |
| **Migrations** | Alembic |

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

Model names, retrieval tuning, and upload limits are configured in `config.yaml`.

---

## Features

- **File upload** — PDF, DOC, DOCX, TXT. Drag & drop supported.
- **URL scraping** — paste any URL and chat with the page content.
- **JSON API ingestion** — point it at any API endpoint, add auth headers if needed.
- **Cited answers** — every answer shows source file, page number, and OCR confidence.
- **Advanced Mode** — toggle in the chat UI for deeper, more thorough answers.
- **Multi-turn chat** — follow-up questions ("explain that", "yup") use conversation context.
- **Real-time upload progress** — each upload step streams live to the UI.

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check |
| `POST` | `/upload/files` | Upload files — streams SSE progress |
| `POST` | `/upload/url` | Scrape a URL — streams SSE progress |
| `POST` | `/upload/api` | Fetch a JSON API with optional headers |
| `POST` | `/chat/` | Ask a question — returns answer + citations |

---

## Project Structure

```
app/
├── main.py              # FastAPI app
├── config.py            # Loads config.yaml + .env
├── routers/
│   ├── upload.py        # Upload endpoints (SSE streaming)
│   └── chat.py          # Chat endpoint
├── services/
│   ├── rag.py           # Answer pipeline
│   ├── ingestion.py     # File parsing, URL scraping, API fetch
│   ├── vector_store.py  # Embedding + vector store
│   └── dedup.py         # Duplicate file detection
├── models/
│   ├── db.py            # SQLAlchemy models
│   └── schemas.py       # Pydantic schemas
├── db/
│   └── session.py       # Database engine
└── static/              # Frontend (HTML/CSS/JS)

alembic/                 # Migrations
config.yaml              # Model names, retrieval params, limits
tests/                   # Test suite (31 tests)
pyproject.toml           # Dependencies
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

# RAG Data Assistant — Master Plan

## Goal
A production-ready RAG chatbot that lets users upload PDFs/docs, scrape URLs, or connect APIs — then ask questions about their data using AI. Deployed free on HuggingFace Spaces.

---

## Tech Stack (Current)

| Layer | Technology |
|---|---|
| Backend | Python 3.13, FastAPI, uvicorn |
| LLM | Groq — llama-3.1-8b-instant (primary, fast) → 4 Groq fallback models → OpenAI gpt-4o-mini (last resort) |
| Intent Check | Groq llama-3.1-8b-instant — dedicated fast model, YES/NO only, runs in parallel with HyDE |
| Embeddings | BAAI/bge-large-en-v1.5 via HuggingFace Inference API (free, primary) / OpenAI text-embedding-3-small (fallback) |
| RAG Pipeline | LangGraph state machine — Intent → HyDE/QueryRewrite → BM25+MMR+RRF+Rerank → LLM answer |
| Hybrid Search | BM25 (keyword) + MMR (semantic) → RRF merge → BGE reranker (BAAI/bge-reranker-large) |
| Graph DB | Neo4j AuraDB — entity/relationship traversal, advanced mode only |
| Vector Store | ChromaDB local (free, HF Spaces) / pgvector on Supabase (production) |
| Document Parsing | LiteParse (Rust-based, page-level metadata) |
| URL Scraping | Crawl4AI (primary) + Playwright (fallback) |
| Frontend | Plain HTML + CSS + Vanilla JS (Stage A) |
| Conversation History | Per-session in-memory, last 10 turns, threadsafe (enables "yup" / follow-up replies) |
| Answer Cache | In-memory MD5-keyed cache, 256 entries max, scoped per session+question+mode |
| Packaging | uv + pyproject.toml + uv.lock |
| Container | Docker (Python 3.13-slim, non-root user) |
| Deployment | HuggingFace Spaces (free) |

---

## Phase Overview

| Phase | What | Area | Status |
|---|---|---|---|
| 1 | uv + Docker foundation | Backend | ✅ Done |
| 2 | FastAPI backend — routers, ingestion, RAG pipeline | Backend | ✅ Done |
| 3 | Frontend — vanilla JS chat UI, drag & drop, SSE streaming | Frontend | ✅ Done |
| 4 | Page tracking, confidence scores, Groq LLM, HF Spaces deploy | Backend + Deploy | ✅ Done |
| 5 | Smart deduplication — skip re-embedding duplicate files | Backend | 📋 Planned |
| 6 | Persistent sessions (Supabase), faster PDF extraction | Backend | 🔜 Next |
| 7 | LangGraph RAG pipeline — Intent, HyDE, QueryRewrite, Neo4j, Hybrid Search | Backend | ✅ Done |
| 7e | Conversation history — per-session, last 10 turns, context-aware follow-ups | Backend | ✅ Done |
| 8 | Groq model fallback chain — 5 models → OpenAI, auto-retry-free | Backend | ✅ Done |
| 9 | Embedding batch optimisation — 7.5× faster upload via batched HF API calls | Backend | ✅ Done |
| 10 | Latency optimisation — parallel intent+HyDE, fast intent model, answer cache | Backend | ✅ Done |
| 11 | React frontend (Stage B), mobile responsive | Frontend | 📋 Planned |

---

## How to Run

```bash
# Local development
uv run uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload

# Docker
docker build -t rag-assistant .
docker run -p 8001:8001 --env-file .env rag-assistant
```

---

## Environment Variables (`.env`)

```env
# LLM — Groq (primary, free)
GROQ_API_KEY=gsk_...
# Model config lives in config.yaml (primary + fallback chain)

# OpenAI — last-resort fallback when all Groq models are rate-limited
OPENAI_API_KEY=sk-...   # optional — only needed if openai_fallback_enabled: true in config.yaml

# Embeddings — BGE via HuggingFace (free, primary)
EMBEDDING_PROVIDER=bge
HUGGINGFACE_API_KEY=hf_...
HUGGINGFACE_MODEL_ID=BAAI/bge-large-en-v1.5

# Vector Store — Supabase pgvector (optional, falls back to ChromaDB)
DB_HOST=
DB_PORT=5432
DB_USER=
DB_PASSWORD=
DB_NAME=

# Neo4j AuraDB — for advanced mode graph traversal (optional)
NEO4J_URI=neo4j+s://xxxxx.databases.neo4j.io
NEO4J_USER=neo4j
NEO4J_PASSWORD=

# Retrieval tuning (also in config.yaml)
RETRIEVAL_K=3
RETRIEVAL_FETCH_K=10
RETRIEVAL_LAMBDA=0.7

# Ingestion limits
MAX_FILE_SIZE_MB=15
MAX_FILES_PER_SESSION=5
MAX_SESSION_SIZE_MB=50
```

---

## Detailed Plans

- Backend details → [PLAN-backend.md](PLAN-backend.md)
- Frontend details → [PLAN-frontend.md](PLAN-frontend.md)

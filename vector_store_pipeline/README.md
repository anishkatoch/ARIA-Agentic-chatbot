# vector_store_pipeline

Self-contained vector store layer: embedding provider selection with
automatic fallback (OpenAI / Voyage AI, with quota-aware model rotation
for Voyage), and a vector store factory (pgvector when a Postgres DB is
configured, local ChromaDB otherwise).

This folder has no dependency on the rest of this repo - copy it into any
other Python project as-is.

## Usage

```python
from vector_store_pipeline import get_vector_store, session_has_data, delete_vectors_by_source

store = get_vector_store("my-session-id")   # pgvector if DB_HOST is set, else local Chroma
store.add_texts(["some chunk"], metadatas=[{"source": "doc.pdf"}])

session_has_data("my-session-id")            # True once something's been added
delete_vectors_by_source(store, "doc.pdf")   # remove every chunk from that source
```

Install deps: `pip install -r vector_store_pipeline/requirements.txt`

## Configuration (env vars, all optional)

| Var | Default | Notes |
|---|---|---|
| `DB_HOST` | unset | leave unset to use local ChromaDB instead of pgvector |
| `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | `5432` / `postgres` / `` / `postgres` | |
| `CHROMA_PERSIST_DIR` | `./chroma_data` | local Chroma storage path |
| `EMBEDDING_PROVIDER` | `openai` | `openai` or `voyage` - tried first, the other is the automatic fallback |
| `EMBEDDING_DIM` | `1024` | must match whatever's already stored in an existing collection |
| `OPENAI_EMBED_MODEL` / `OPENAI_API_KEY` | `text-embedding-3-small` | also used as the resilience fallback for any other provider |
| `VOYAGE_API_KEY` / `VOYAGE_MODEL_ID` / `VOYAGE_OUTPUT_DIMENSION` | - / `voyage-3.5-lite` / `1024` | |
| `VOYAGE_MODELS` | `[]` | JSON priority list, e.g. `[{"model": "voyage-3.5-lite", "free_tokens": 200000000}, {"model": "voyage-3.5"}]` |

Voyage usage/quota state is tracked in a Postgres table
(`embedding_model_usage`, auto-created) when `DB_HOST` is set; without a DB
it still works, it just doesn't remember usage across restarts.

import logging
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings
from langchain_postgres.vectorstores import PGVector
from langchain_chroma import Chroma

from app.config import cfg

logger = logging.getLogger(__name__)

# One shared connection pool for every session's PGVector store, instead of
# each session opening (and cold-starting) its own pool against Supabase.
_db_engine: Engine | None = None


def _get_db_engine() -> Engine:
    global _db_engine
    if _db_engine is None:
        _db_engine = create_engine(cfg.db_url, pool_pre_ping=True)
    return _db_engine


_PROVIDERS = {}  # populated below, after each _try_* is defined

# Embedding provider selection doesn't depend on session_id, so one instance
# serves every session - skips re-resolving the provider (incl. a DB lookup
# for Voyage's active-model tracking) on every call.
_embeddings_cache = None


def get_embeddings():
    global _embeddings_cache
    if _embeddings_cache is not None:
        return _embeddings_cache

    logger.info(f"[EMBED] Provider priority: {cfg.embedding_provider}")
    primary = _PROVIDERS.get(cfg.embedding_provider, _try_bge)
    fallbacks = [fn for name, fn in _PROVIDERS.items() if fn is not primary]
    for try_fn in [primary, *fallbacks]:
        embeddings = try_fn()
        if embeddings:
            provider_name = next((n for n, fn in _PROVIDERS.items() if fn is try_fn), "unknown")
            # Wraps whichever provider was actually selected so a LIVE
            # embed_documents/embed_query call failing mid-ingest (not just
            # unavailable at startup - e.g. HuggingFace/Voyage returning a
            # 503 or running out of quota partway through a batch) also
            # falls back to OpenAI, not only the one-time provider pick above.
            wrapped = _ResilientEmbeddings(embeddings, provider_name)
            # Only cache the primary provider. If it's temporarily down and a
            # fallback had to be used instead, don't cache that fallback -
            # otherwise it would stick for the rest of the process's life
            # even after the primary recovers (e.g. Voyage's quota resets or
            # its payment method issue is fixed), defeating the whole point
            # of _DynamicVoyageEmbeddings' auto-recovery design.
            if try_fn is primary:
                _embeddings_cache = wrapped
            return wrapped
    return None


class _ResilientEmbeddings(Embeddings):
    """Wraps whichever embedding provider get_embeddings() selected and
    transparently falls back to OpenAI's small text-embedding model
    (cfg.openai_embed_model, default "text-embedding-3-small") if a live
    embed_documents/embed_query call fails - e.g. the HuggingFace Inference
    API or Voyage API being temporarily down, rate-limited, or out of quota
    mid-ingest, which the one-time provider selection in get_embeddings()
    above can't catch since it only probes availability once at startup.

    The fallback is constructed with OpenAI's native `dimensions` parameter
    set to cfg.embedding_dim (the primary provider's configured dimension),
    so a fallback vector stays dimensionally compatible with whatever's
    already stored in this session's collection - without this, a partial
    fallback mid-batch would corrupt the collection (most vector stores
    reject mismatched-width vectors outright, but a successful Chroma/
    pgvector write of the wrong width would silently break future
    similarity search instead).

    Only engages when OPENAI_API_KEY is configured and the primary isn't
    already OpenAI (nothing meaningful to fall back to in that case)."""

    def __init__(self, primary: Embeddings, primary_name: str):
        self._primary = primary
        self._primary_name = primary_name
        self._fallback: Embeddings | None = None

    def _get_fallback(self) -> Embeddings | None:
        if self._primary_name == "openai" or not cfg.openai_api_key:
            return None
        if self._fallback is None:
            self._fallback = OpenAIEmbeddings(
                model=cfg.openai_embed_model,
                openai_api_key=cfg.openai_api_key,
                dimensions=cfg.embedding_dim,
            )
        return self._fallback

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        try:
            return self._primary.embed_documents(texts)
        except Exception as e:
            fallback = self._get_fallback()
            if fallback is None:
                raise
            logger.warning(
                f"[EMBED] {self._primary_name} embed_documents failed ({e}) - "
                f"falling back to OpenAI {cfg.openai_embed_model}"
            )
            return fallback.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        try:
            return self._primary.embed_query(text)
        except Exception as e:
            fallback = self._get_fallback()
            if fallback is None:
                raise
            logger.warning(
                f"[EMBED] {self._primary_name} embed_query failed ({e}) - "
                f"falling back to OpenAI {cfg.openai_embed_model}"
            )
            return fallback.embed_query(text)


def _try_bge():
    try:
        from langchain_huggingface import HuggingFaceEndpointEmbeddings
        embeddings = HuggingFaceEndpointEmbeddings(
            model=cfg.hf_model_id,
            huggingfacehub_api_token=cfg.hf_api_key,
        )
        logger.info(f"[EMBED] Using {cfg.hf_model_id} (HuggingFace Inference API, {cfg.embedding_dim} dims)")
        return embeddings
    except Exception as e:
        logger.warning(f"[EMBED] BGE unavailable ({e})")
        return None


def _try_openai():
    try:
        embeddings = OpenAIEmbeddings(
            model=cfg.openai_embed_model,
            openai_api_key=cfg.openai_api_key,
        )
        logger.info(f"[EMBED] Using {cfg.openai_embed_model} (OpenAI)")
        return embeddings
    except Exception as e:
        logger.warning(f"[EMBED] OpenAI unavailable ({e})")
        return None


class _DynamicVoyageEmbeddings(Embeddings):
    """Voyage embeddings that automatically switch to the next model in
    cfg.voyage_models once the active one's free quota is exhausted (tracked
    in the embedding_model_usage DB table) or a call against it fails
    outright. All configured models are forced to the same output dimension
    so switching never produces vectors incompatible with what's already
    stored in the vector store."""

    def __init__(self, models: list[dict]):
        self._models = models or [{"model": cfg.voyage_model_id, "free_tokens": None}]

    def _embed(self, texts: list[str]) -> list[list[float]]:
        from app.services.embedding_usage import pick_active_model, record_usage, mark_exhausted, mark_broken
        import voyageai

        client = voyageai.Client(api_key=cfg.voyage_api_key)
        tried: set[str] = set()
        last_exc: Exception | None = None

        while True:
            model = pick_active_model(self._models, provider="voyage")
            if model is None or model in tried:
                break
            tried.add(model)
            try:
                result = client.embed(texts, model=model, output_dimension=cfg.voyage_output_dimension)
                record_usage(model, result.total_tokens, provider="voyage")
                return result.embeddings
            except Exception as e:
                last_exc = e
                http_status = getattr(e, "http_status", None)
                if http_status == 402:
                    # Payment Required - the model's free quota is actually used up.
                    mark_exhausted(model, provider="voyage")
                elif http_status == 429:
                    # Rate-limited (e.g. no payment method on file yet) - transient,
                    # don't disable the model, just move on for this call.
                    logger.warning(f"[EMBED] Voyage model {model} rate-limited ({e}), trying next")
                else:
                    mark_broken(model, str(e), provider="voyage")
                logger.warning(f"[EMBED] Voyage model {model} failed ({e}), trying next")

        raise RuntimeError(f"All configured Voyage models exhausted or unavailable. Last error: {last_exc}")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text])[0]


def _try_voyage():
    try:
        if not cfg.voyage_api_key:
            raise ValueError("VOYAGE_API_KEY not set")
        from app.services.embedding_usage import pick_active_model
        models = cfg.voyage_models or [{"model": cfg.voyage_model_id, "free_tokens": None}]
        active = pick_active_model(models, provider="voyage")
        if active is None:
            raise RuntimeError("All configured Voyage models have exhausted their free quota or are broken")
        embeddings = _DynamicVoyageEmbeddings(models)
        logger.info(f"[EMBED] Using Voyage AI - active model: {active} ({cfg.voyage_output_dimension} dims, auto-fallback across {[m['model'] for m in models]})")
        return embeddings
    except Exception as e:
        logger.warning(f"[EMBED] Voyage unavailable ({e})")
        return None


_PROVIDERS = {
    "bge":    _try_bge,
    "openai": _try_openai,
    "voyage": _try_voyage,
}


# Once a session has data it only ever gains more (nothing deletes documents
# from a live session), so "has data" never flips back to False - caching
# only the True result sidesteps needing to invalidate this from every
# upload/ingest call site.
_has_data_cache: dict[str, bool] = {}


def session_has_data(session_id: str) -> bool:
    """Return True if the session's vector store has at least one document."""
    if _has_data_cache.get(session_id):
        return True
    try:
        store = get_vector_store(session_id)
        if hasattr(store, "_collection"):
            # Chroma - local count, no embedding call needed.
            result = store._collection.count() > 0
        else:
            result = len(store.similarity_search("a", k=1)) > 0
    except Exception:
        result = False
    if result:
        _has_data_cache[session_id] = True
    return result


# Connecting PGVector/Chroma re-runs schema setup (CREATE EXTENSION/TABLE IF
# NOT EXISTS, etc.) against the DB every time it's constructed - caching per
# session means that cost is paid once per process, not once per chat message.
_store_cache: dict[str, object] = {}


def get_vector_store(session_id: str):
    if session_id in _store_cache:
        return _store_cache[session_id]

    logger.info(f"[STORE] Initialising vector store - session={session_id}")
    embeddings = get_embeddings()
    if not embeddings:
        logger.error("[STORE] No embedding provider available")
        raise RuntimeError("No embedding provider available. Check VOYAGE_API_KEY, HUGGINGFACE_API_KEY, or OPENAI_API_KEY.")

    collection = f"session_{session_id}"

    if cfg.db_url:
        try:
            store = PGVector(
                embeddings=embeddings,
                collection_name=collection,
                connection=_get_db_engine(),
            )
            logger.info(f"[STORE] Connected to pgvector (Supabase) - collection={collection}")
            _store_cache[session_id] = store
            return store
        except Exception as e:
            logger.warning(f"[STORE] pgvector unavailable ({e}) - falling back to ChromaDB")

    # Not cached: a transient pgvector outage must not permanently pin this
    # session to Chroma for the rest of the process's life. Leaving this
    # uncached means the next call retries pgvector fresh, instead of this
    # session's documents silently ending up split across two different
    # backing stores once Supabase recovers.
    store = Chroma(
        collection_name=collection,
        embedding_function=embeddings,
        persist_directory="./chroma_data",
    )
    logger.info(f"[STORE] Using ChromaDB (local) - collection={collection}")
    return store


def delete_vectors_by_source(store, filename: str) -> int:
    """Removes every chunk tagged with metadata source==filename (matched
    case-insensitively - see below) from this session's vector store - used
    when a user detaches a document from a chat (see app.routers.upload's
    DELETE /upload/session-documents).

    Matching is case-insensitive because the `documents` table stores
    filenames lowercased (see Document.filename's comment in app/models/db.py
    and every _insert_pending()/filename.lower() call site), but the vector
    chunks' "source" metadata is tagged with the filename in its original
    upload case - comparing exactly would silently leave orphaned chunks
    behind for any mixed-case filename.

    Chroma has no case-insensitive filter in its own "where" query language,
    so this fetches all chunks' metadata and filters in Python instead -
    O(n) in collection size, but this only runs on an explicit user "detach"
    click, not a hot path. PGVector's public .delete() only accepts explicit
    ids (no metadata filter at all, checked against the installed
    langchain_postgres version), so this runs a raw SQL delete against its
    underlying langchain_pg_embedding/langchain_pg_collection tables instead -
    stable, documented table/column names that have been consistent across
    langchain_postgres versions, unlike undocumented internal attributes."""
    target = filename.lower()

    if hasattr(store, "_collection"):
        data = store._collection.get(include=["metadatas"])
        ids = [
            id_ for id_, meta in zip(data.get("ids", []), data.get("metadatas", []))
            if (meta or {}).get("source", "").lower() == target
        ]
        if ids:
            store._collection.delete(ids=ids)
        return len(ids)

    collection_name = getattr(store, "collection_name", None)
    if not collection_name:
        logger.warning(f"[STORE] delete_vectors_by_source: couldn't resolve collection name for {filename}")
        return 0

    from sqlalchemy import text
    with _get_db_engine().begin() as conn:
        result = conn.execute(
            text(
                "DELETE FROM langchain_pg_embedding e "
                "USING langchain_pg_collection c "
                "WHERE e.collection_id = c.uuid AND c.name = :collection "
                "AND LOWER(e.cmetadata->>'source') = :filename"
            ),
            {"collection": collection_name, "filename": target},
        )
        return result.rowcount

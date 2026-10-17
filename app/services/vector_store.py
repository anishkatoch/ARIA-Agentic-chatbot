import logging
from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings
from langchain_postgres.vectorstores import PGVector
from langchain_chroma import Chroma

from app.config import cfg

logger = logging.getLogger(__name__)


_PROVIDERS = {}  # populated below, after each _try_* is defined


def get_embeddings():
    logger.info(f"[EMBED] Provider priority: {cfg.embedding_provider}")
    primary = _PROVIDERS.get(cfg.embedding_provider, _try_bge)
    fallbacks = [fn for name, fn in _PROVIDERS.items() if fn is not primary]
    for try_fn in [primary, *fallbacks]:
        embeddings = try_fn()
        if embeddings:
            return embeddings
    return None


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
                err = str(e).lower()
                if any(s in err for s in ("credit", "402", "payment", "quota")):
                    mark_exhausted(model, provider="voyage")
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
        logger.info(f"[EMBED] Using Voyage AI — active model: {active} ({cfg.voyage_output_dimension} dims, auto-fallback across {[m['model'] for m in models]})")
        return embeddings
    except Exception as e:
        logger.warning(f"[EMBED] Voyage unavailable ({e})")
        return None


_PROVIDERS = {
    "bge":    _try_bge,
    "openai": _try_openai,
    "voyage": _try_voyage,
}


def session_has_data(session_id: str) -> bool:
    """Return True if the session's vector store has at least one document."""
    try:
        store = get_vector_store(session_id)
        results = store.similarity_search("a", k=1)
        return len(results) > 0
    except Exception:
        return False


def get_vector_store(session_id: str):
    logger.info(f"[STORE] Initialising vector store — session={session_id}")
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
                connection=cfg.db_url,
            )
            logger.info(f"[STORE] Connected to pgvector (Supabase) — collection={collection}")
            return store
        except Exception as e:
            logger.warning(f"[STORE] pgvector unavailable ({e}) — falling back to ChromaDB")

    store = Chroma(
        collection_name=collection,
        embedding_function=embeddings,
        persist_directory="./chroma_data",
    )
    logger.info(f"[STORE] Using ChromaDB (local) — collection={collection}")
    return store

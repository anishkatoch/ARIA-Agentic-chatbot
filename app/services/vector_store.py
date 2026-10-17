import logging
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


def _try_voyage():
    try:
        if not cfg.voyage_api_key:
            raise ValueError("VOYAGE_API_KEY not set")
        from langchain_voyageai import VoyageAIEmbeddings
        embeddings = VoyageAIEmbeddings(
            model=cfg.voyage_model_id,
            voyage_api_key=cfg.voyage_api_key,
        )
        logger.info(f"[EMBED] Using {cfg.voyage_model_id} (Voyage AI, {cfg.embedding_dim} dims)")
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

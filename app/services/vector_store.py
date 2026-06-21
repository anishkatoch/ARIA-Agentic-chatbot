import logging
from langchain_openai import OpenAIEmbeddings
from langchain_postgres.vectorstores import PGVector
from langchain_chroma import Chroma

from app.config import cfg

logger = logging.getLogger(__name__)


def get_embeddings():
    logger.info(f"[EMBED] Provider priority: {cfg.embedding_provider}")
    if cfg.embedding_provider == "bge":
        return _try_bge() or _try_openai()
    else:
        return _try_openai() or _try_bge()


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


def get_vector_store(session_id: str):
    logger.info(f"[STORE] Initialising vector store — session={session_id}")
    embeddings = get_embeddings()
    if not embeddings:
        logger.error("[STORE] No embedding provider available")
        raise RuntimeError("No embedding provider available. Check HUGGINGFACE_API_KEY or OPENAI_API_KEY.")

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

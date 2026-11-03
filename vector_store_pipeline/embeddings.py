import logging

from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings

from .config import cfg

logger = logging.getLogger(__name__)

_PROVIDERS = {}  # populated below, after each _try_* is defined

# Embedding provider selection doesn't depend on session_id, so one instance
# serves every caller - skips re-resolving the provider (incl. a DB lookup
# for Voyage's active-model tracking) on every call.
_embeddings_cache = None


def get_embeddings():
    global _embeddings_cache
    if _embeddings_cache is not None:
        return _embeddings_cache

    logger.info(f"[EMBED] Provider priority: {cfg.embedding_provider}")
    primary = _PROVIDERS.get(cfg.embedding_provider, _try_openai)
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
    already stored in the vector store - without this, a partial fallback
    mid-batch would corrupt the collection (most vector stores reject
    mismatched-width vectors outright, but a successful Chroma/pgvector
    write of the wrong width would silently break future similarity search
    instead).

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
    in the embedding_model_usage DB table, see usage_tracking.py) or a call
    against it fails outright. All configured models are forced to the same
    output dimension so switching never produces vectors incompatible with
    what's already stored in the vector store."""

    def __init__(self, models: list[dict]):
        self._models = models or [{"model": cfg.voyage_model_id, "free_tokens": None}]

    def _embed(self, texts: list[str]) -> list[list[float]]:
        from .usage_tracking import pick_active_model, record_usage, mark_exhausted, mark_broken
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
        from .usage_tracking import pick_active_model
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
    "openai": _try_openai,
    "voyage": _try_voyage,
}

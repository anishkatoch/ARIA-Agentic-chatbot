import json
import os
from urllib.parse import quote_plus


class VectorStoreConfig:
    """Standalone defaults for this package - no dependency on any host
    app's config system. Everything is read from env vars with sane
    defaults, mirroring a typical config.yaml's [embeddings]/[db] sections."""

    def __init__(self):
        # ── Postgres (pgvector) - optional, falls back to local Chroma ──
        self.db_host: str = os.getenv("DB_HOST", "")
        self.db_port: str = os.getenv("DB_PORT", "5432")
        self.db_user: str = os.getenv("DB_USER", "postgres")
        self.db_password: str = os.getenv("DB_PASSWORD", "")
        self.db_name: str = os.getenv("DB_NAME", "postgres")
        self.chroma_persist_dir: str = os.getenv("CHROMA_PERSIST_DIR", "./chroma_data")

        # ── Embedding provider priority + per-provider settings ─────────
        self.embedding_provider: str = os.getenv("EMBEDDING_PROVIDER", "openai")
        self.embedding_dim: int = int(os.getenv("EMBEDDING_DIM", "1024"))

        self.openai_embed_model: str = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")
        self.openai_api_key: str = os.getenv("OPENAI_API_KEY", "")

        self.voyage_api_key: str = os.getenv("VOYAGE_API_KEY", "")
        self.voyage_model_id: str = os.getenv("VOYAGE_MODEL_ID", "voyage-3.5-lite")
        self.voyage_output_dimension: int = int(os.getenv("VOYAGE_OUTPUT_DIMENSION", "1024"))
        # Priority-ordered fallback chain with free_tokens caps, e.g.
        # '[{"model": "voyage-3.5-lite", "free_tokens": 200000000}, {"model": "voyage-3.5"}]'
        self.voyage_models: list = json.loads(os.getenv("VOYAGE_MODELS", "[]"))

    @property
    def db_url(self) -> str | None:
        if not self.db_host:
            return None
        user = quote_plus(self.db_user)
        password = quote_plus(self.db_password)
        base = f"postgresql://{user}:{password}@{self.db_host}:{self.db_port}/{self.db_name}"
        localhost = self.db_host in ("localhost", "127.0.0.1", "::1")
        return base if localhost else f"{base}?sslmode=require"


cfg = VectorStoreConfig()

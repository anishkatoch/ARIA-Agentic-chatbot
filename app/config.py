import os
import yaml
from pathlib import Path
from urllib.parse import quote_plus
from dotenv import load_dotenv

load_dotenv()

_ROOT = Path(__file__).parent.parent
_yaml_path = _ROOT / "config.yaml"

try:
    _y = yaml.safe_load(_yaml_path.read_text())
except FileNotFoundError:
    raise RuntimeError(
        f"config.yaml not found at {_yaml_path}. "
        "Copy config.yaml from the repo root and adjust as needed."
    )


class _Config:
    def __init__(self, y: dict):
        app = y.get("app", {})
        llm = y.get("llm", {})
        emb = y.get("embeddings", {})
        ret = y.get("retrieval", {})
        lim = y.get("limits", {})
        graph = y.get("graph", {})

        # ── App ───────────────────────────────────────────────────
        self.port: int = int(app.get("port", 8001))

        # ── LLM ───────────────────────────────────────────────────
        self.llm_provider: str             = llm.get("provider", "groq")
        self.groq_model: str               = llm.get("groq_model", "llama-3.3-70b-versatile")
        self.groq_intent_model: str        = llm.get("intent_model", "llama-3.1-8b-instant")
        self.groq_fallback_models: list    = llm.get("groq_fallback_models", [])
        self.openai_llm_model: str         = llm.get("openai_model", "gpt-4o-mini")
        self.openai_fallback_enabled: bool = bool(llm.get("openai_fallback_enabled", False))

        # ── Embeddings ────────────────────────────────────────────
        self.embedding_provider: str = emb.get("provider", "openai")
        # hf_model_id is no longer used for embeddings (BGE removed as an
        # embedding provider) - kept only because app/services/rag.py's HF
        # reranker fallback (_rerank_hf) reuses this same free model id.
        self.hf_model_id: str        = emb.get("bge_model_id", "sentence-transformers/all-MiniLM-L6-v2")
        self.openai_embed_model: str = emb.get("openai_model", "text-embedding-3-small")
        self.voyage_model_id: str    = emb.get("voyage_model_id", "voyage-3.5-lite")
        self.voyage_models: list     = emb.get("voyage_models", [])  # priority-ordered fallback chain with free_tokens caps
        self.voyage_output_dimension: int = int(emb.get("voyage_output_dimension", 1024))
        self.embedding_dim: int      = int(emb.get("dimension", 1024))

        # ── Retrieval ─────────────────────────────────────────────
        self.retrieval_k: int        = int(ret.get("k", 3))
        self.retrieval_fetch_k: int  = int(ret.get("fetch_k", 10))
        self.retrieval_lambda: float = float(ret.get("lambda_mult", 0.7))
        self.hybrid_top_k: int       = int(ret.get("hybrid_top_k", 7))
        self.rerank_top_n: int       = int(ret.get("rerank_top_n", 5))
        self.reranker_model: str     = ret.get("reranker_model", "BAAI/bge-reranker-large")
        self.cohere_rerank_model: str = ret.get("cohere_rerank_model", "rerank-v3.5")

        # ── Limits ────────────────────────────────────────────────
        self.max_file_size_mb: int      = int(lim.get("max_file_size_mb", 15))
        self.max_files_per_session: int = int(lim.get("max_files_per_session", 5))
        self.max_session_size_mb: int   = int(lim.get("max_session_size_mb", 50))
        self.dedup_threshold_mb: float  = float(lim.get("dedup_threshold_mb", 2))
        self.gmail_bulk_ingest_limit: int = int(lim.get("gmail_bulk_ingest_limit", 500))
        self.zip_max_entries: int       = int(lim.get("zip_max_entries", 200))

        # ── Secrets - from .env only ─────────────────────────────
        self.groq_api_key: str    = os.getenv("GROQ_API_KEY", "")
        self.openai_api_key: str  = os.getenv("OPENAI_API_KEY", "")
        self.hf_api_key: str      = os.getenv("HUGGINGFACE_API_KEY", "")
        self.voyage_api_key: str  = os.getenv("VOYAGE_API_KEY", "")
        self.cohere_api_key: str  = os.getenv("COHERE_API_KEY", "")

        self.db_host: str     = os.getenv("DB_HOST", "")
        self.db_port: str     = os.getenv("DB_PORT", "5432")
        self.db_user: str     = os.getenv("DB_USER", "")
        self.db_password: str = os.getenv("DB_PASSWORD", "")
        self.db_name: str     = os.getenv("DB_NAME", "")

        self.supabase_url: str          = os.getenv("SUPABASE_URL", "")
        self.supabase_anon_key: str     = os.getenv("SUPABASE_ANON_KEY", "")
        self.supabase_service_key: str  = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

        # Supabase Storage bucket backing the default/sample chat session -
        # files dropped here replace the bundled sample doc as what a user
        # with no uploads of their own talks to. Polled every
        # default_kb_sync_interval_s so new/changed files there are picked
        # up without a server restart.
        self.default_kb_bucket: str            = os.getenv("DEFAULT_KB_BUCKET", "aria")
        self.default_kb_sync_interval_s: int    = int(os.getenv("DEFAULT_KB_SYNC_INTERVAL_S", "120"))

        # ── Auth / JWT ────────────────────────────────────────────
        self.jwt_secret: str     = os.getenv("JWT_SECRET", "change-me-in-production-use-a-long-random-string")
        self.jwt_algorithm: str  = "HS256"
        self.jwt_expire_hours: int = 24

        # ── Admin portal (single fixed super-admin account, set in .env) ──
        self.admin_email: str    = os.getenv("ADMIN_EMAIL", "")
        self.admin_password: str = os.getenv("ADMIN_PASSWORD", "")

        # ── Neo4j (graph DB - primary) ───────────────────────────────
        self.neo4j_uri: str      = os.getenv("NEO4J_URI", "")
        self.neo4j_user: str     = os.getenv("NEO4J_USER", "neo4j")
        self.neo4j_password: str = os.getenv("NEO4J_PASSWORD", "")

        # ── Azure Cosmos DB / Gremlin API (graph DB - fallback) ──────
        # Used automatically when Neo4j above is unset or unreachable.
        # Database/graph names come from config.yaml (not secrets);
        # endpoint/key are secrets and stay in .env.
        self.cosmos_gremlin_endpoint: str  = os.getenv("COSMOS_GREMLIN_ENDPOINT", "")
        self.cosmos_gremlin_key: str       = os.getenv("COSMOS_GREMLIN_KEY", "")
        self.cosmos_gremlin_database: str  = graph.get("cosmos_gremlin_database", "aria-graphdb")
        self.cosmos_gremlin_graph: str     = graph.get("cosmos_gremlin_graph", "aria-graph")

    @property
    def db_url(self) -> str | None:
        if not self.db_host:
            return None
        user     = quote_plus(self.db_user)
        password = quote_plus(self.db_password)
        base = f"postgresql://{user}:{password}@{self.db_host}:{self.db_port}/{self.db_name}"
        # Supabase (and most remote Postgres) requires SSL
        localhost = self.db_host in ("localhost", "127.0.0.1", "::1")
        return base if localhost else f"{base}?sslmode=require"


cfg = _Config(_y)

# ── Bridge config.yaml's embedding settings into the env vars that the
# portable vector_store_pipeline package reads. That package is designed to
# be copy-pasteable into any project and only knows about env vars, not this
# app's config.yaml - without this, it silently falls back to its own
# built-in defaults instead of what config.yaml actually says (e.g. it would
# never learn about the Voyage model priority list below). setdefault() so
# an operator's own explicit env var (if any) still wins.
import json as _json
os.environ.setdefault("EMBEDDING_PROVIDER", cfg.embedding_provider)
os.environ.setdefault("EMBEDDING_DIM", str(cfg.embedding_dim))
os.environ.setdefault("OPENAI_EMBED_MODEL", cfg.openai_embed_model)
os.environ.setdefault("VOYAGE_MODEL_ID", cfg.voyage_model_id)
os.environ.setdefault("VOYAGE_MODELS", _json.dumps(cfg.voyage_models))
os.environ.setdefault("VOYAGE_OUTPUT_DIMENSION", str(cfg.voyage_output_dimension))

# Fixed session ID for the built-in sample document
DEFAULT_SESSION_ID = "aria_default_sample"

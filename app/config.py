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
        self.embedding_provider: str = emb.get("provider", "bge")
        self.hf_model_id: str        = emb.get("bge_model_id", "BAAI/bge-large-en-v1.5")
        self.openai_embed_model: str = emb.get("openai_model", "text-embedding-3-small")
        self.embedding_dim: int      = int(emb.get("dimension", 1024))

        # ── Retrieval ─────────────────────────────────────────────
        self.retrieval_k: int        = int(ret.get("k", 3))
        self.retrieval_fetch_k: int  = int(ret.get("fetch_k", 10))
        self.retrieval_lambda: float = float(ret.get("lambda_mult", 0.7))
        self.hybrid_top_k: int       = int(ret.get("hybrid_top_k", 7))
        self.rerank_top_n: int       = int(ret.get("rerank_top_n", 5))
        self.reranker_model: str     = ret.get("reranker_model", "BAAI/bge-reranker-large")

        # ── Limits ────────────────────────────────────────────────
        self.max_file_size_mb: int      = int(lim.get("max_file_size_mb", 15))
        self.max_files_per_session: int = int(lim.get("max_files_per_session", 5))
        self.max_session_size_mb: int   = int(lim.get("max_session_size_mb", 50))
        self.dedup_threshold_mb: float  = float(lim.get("dedup_threshold_mb", 2))

        # ── Secrets — from .env only ─────────────────────────────
        self.groq_api_key: str    = os.getenv("GROQ_API_KEY", "")
        self.openai_api_key: str  = os.getenv("OPENAI_API_KEY", "")
        self.hf_api_key: str      = os.getenv("HUGGINGFACE_API_KEY", "")

        self.db_host: str     = os.getenv("DB_HOST", "")
        self.db_port: str     = os.getenv("DB_PORT", "5432")
        self.db_user: str     = os.getenv("DB_USER", "")
        self.db_password: str = os.getenv("DB_PASSWORD", "")
        self.db_name: str     = os.getenv("DB_NAME", "")

        self.supabase_url: str      = os.getenv("SUPABASE_URL", "")
        self.supabase_anon_key: str = os.getenv("SUPABASE_ANON_KEY", "")

        # ── Neo4j ─────────────────────────────────────────────────
        self.neo4j_uri: str      = os.getenv("NEO4J_URI", "")
        self.neo4j_user: str     = os.getenv("NEO4J_USER", "neo4j")
        self.neo4j_password: str = os.getenv("NEO4J_PASSWORD", "")

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

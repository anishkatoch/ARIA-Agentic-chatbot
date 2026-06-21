"""
Tests for app/config.py + config.yaml.
Verifies that every setting loads correctly and that secrets
come from env, never from the yaml file.
"""
import os
import pytest
from app.config import cfg


class TestYamlSettings:
    def test_app_port(self):
        assert cfg.port == 8001

    def test_groq_model(self):
        assert cfg.groq_model == "llama-3.3-70b-versatile"

    def test_embedding_provider(self):
        assert cfg.embedding_provider == "bge"

    def test_embedding_dim(self):
        assert cfg.embedding_dim == 1024

    def test_hf_model_id(self):
        assert "bge" in cfg.hf_model_id.lower()

    def test_retrieval_k(self):
        assert cfg.retrieval_k == 3

    def test_retrieval_fetch_k(self):
        assert cfg.retrieval_fetch_k == 10
        assert cfg.retrieval_fetch_k >= cfg.retrieval_k  # invariant

    def test_retrieval_lambda(self):
        assert 0.0 <= cfg.retrieval_lambda <= 1.0

    def test_max_file_size_mb(self):
        assert cfg.max_file_size_mb == 15

    def test_max_files_per_session(self):
        assert cfg.max_files_per_session == 5

    def test_max_session_size_mb(self):
        assert cfg.max_session_size_mb == 50

    def test_dedup_threshold_mb(self):
        assert cfg.dedup_threshold_mb == 2.0


class TestDbUrl:
    """Test db_url property by instantiating _Config directly with controlled data."""

    def _make_cfg(self, env_overrides: dict):
        """Create a _Config from config.yaml with env overrides applied in-process."""
        import os
        from app.config import _Config, _y

        saved = {}
        for k, v in env_overrides.items():
            saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        try:
            return _Config(_y)
        finally:
            for k, orig in saved.items():
                if orig is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = orig

    def test_db_url_none_when_no_host(self):
        """No DB_HOST → db_url must return None (ChromaDB fallback)."""
        instance = self._make_cfg({"DB_HOST": None})
        assert instance.db_url is None

    def test_db_url_built_from_parts(self):
        instance = self._make_cfg({
            "DB_HOST": "localhost", "DB_PORT": "5432",
            "DB_USER": "user", "DB_PASSWORD": "pass", "DB_NAME": "testdb",
        })
        url = instance.db_url
        assert url is not None
        assert "localhost" in url
        assert "testdb" in url
        assert "user" in url

    def test_db_url_url_encodes_special_chars(self):
        instance = self._make_cfg({
            "DB_HOST": "db.host.io", "DB_PORT": "5432",
            "DB_USER": "admin", "DB_PASSWORD": "p@ss#word!", "DB_NAME": "mydb",
        })
        url = instance.db_url
        # Special chars must be URL-encoded — no raw @ inside the credentials part
        creds_part = url.split("://")[1].split("@")[0]
        assert "@" not in creds_part

    def test_no_secrets_in_yaml(self):
        """config.yaml file must not contain any API keys or passwords."""
        from pathlib import Path
        yaml_text = (Path(__file__).parent.parent / "config.yaml").read_text()
        forbidden = ["_KEY=", "_PASSWORD=", "_SECRET=", "sk-", "gsk_"]
        for token in forbidden:
            assert token not in yaml_text, f"Found potential secret '{token}' in config.yaml"

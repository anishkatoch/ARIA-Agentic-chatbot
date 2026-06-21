"""
Shared fixtures. We build a minimal FastAPI test app (no lifespan/playwright)
so tests run fast and offline with no external dependencies.
"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def test_app():
    """Minimal app with only the routers we test — no playwright lifespan."""
    from app.routers import upload, chat
    app = FastAPI()
    app.include_router(upload.router)
    app.include_router(chat.router)
    return app


@pytest.fixture(scope="session")
def client(test_app):
    with TestClient(test_app) as c:
        yield c


@pytest.fixture
def mock_vector_store():
    store = MagicMock()
    store.add_texts = MagicMock(return_value=["id1"])
    store._collection = MagicMock()
    store._collection.count = MagicMock(return_value=5)
    store._collection.get = MagicMock(return_value={
        "ids": ["id1", "id2"],
        "documents": ["chunk one", "chunk two"],
        "metadatas": [{"source": "test.pdf"}, {"source": "test.pdf"}],
        "embeddings": [[0.1] * 10, [0.2] * 10],
    })
    return store

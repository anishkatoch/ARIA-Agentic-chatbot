from sqlalchemy import (
    Column, String, DateTime, Text, ForeignKey,
    BigInteger, UniqueConstraint, Index
)
from sqlalchemy.orm import declarative_base
from sqlalchemy.dialects.postgresql import UUID
from pgvector.sqlalchemy import Vector
from datetime import datetime, timezone
import uuid

from app.config import cfg

EMBEDDING_DIM = cfg.embedding_dim

Base = declarative_base()


class Session(Base):
    __tablename__ = "sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Document(Base):
    __tablename__ = "documents"

    id           = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id   = Column(UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False)
    client_token = Column(String, nullable=False, index=True)
    filename     = Column(String, nullable=False)          # stored lowercase
    file_size    = Column(BigInteger, nullable=False)       # bytes
    content_hash = Column(String, nullable=False)          # SHA256
    first_chunk  = Column(Text, nullable=True)             # first 500 chars
    middle_chunk = Column(Text, nullable=True)             # middle 500 chars
    last_chunk   = Column(Text, nullable=True)             # last 500 chars
    avg_confidence = Column(String, nullable=True)         # OCR confidence 0-1
    chunks_stored  = Column(BigInteger, nullable=True)     # total vectors written
    source_type  = Column(String, nullable=False, default="file")  # file | url | api
    status       = Column(String, nullable=False, default="pending")  # pending | complete | failed
    uploaded_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("client_token", "content_hash", name="uq_client_token_content_hash"),
        Index("ix_documents_client_token", "client_token"),
    )


class Embedding(Base):
    __tablename__ = "embeddings"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False)
    content     = Column(Text, nullable=False)
    embedding   = Column(Vector(EMBEDDING_DIM))
    created_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ChatHistory(Base):
    __tablename__ = "chat_history"

    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False)
    role       = Column(String, nullable=False)   # "user" | "assistant"
    message    = Column(Text, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class KnowledgeBase(Base):
    """An admin-curated, shared document set. Backed by its own vector-store
    collection (session_id), same mechanism as a regular user session - a KB
    is just a named, admin-owned session that every user can be pointed at."""
    __tablename__ = "knowledge_bases"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name        = Column(String, nullable=False, unique=True)
    description = Column(Text, nullable=True)
    session_id  = Column(String, nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    status      = Column(String, nullable=False, default="active")  # active | inactive
    created_by  = Column(String, nullable=True)  # admin email
    created_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                          onupdate=lambda: datetime.now(timezone.utc))


class KnowledgeBaseDocument(Base):
    """A single file bundled into a KnowledgeBase."""
    __tablename__ = "knowledge_base_documents"

    id                = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    knowledge_base_id = Column(UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=False)
    filename          = Column(String, nullable=False)
    file_size         = Column(BigInteger, nullable=False)
    chunks_stored     = Column(BigInteger, nullable=True)
    status            = Column(String, nullable=False, default="pending")  # pending | complete | failed
    vector_ids        = Column(Text, nullable=True)  # comma-separated vectorstore ids, for targeted delete
    uploaded_at       = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_kb_documents_knowledge_base_id", "knowledge_base_id"),
    )


class EmbeddingModelUsage(Base):
    """Cumulative token usage per embedding model - lets the app auto-switch
    to the next model in the priority list once a model's free quota is used
    up, or once it's been marked broken after a failed call."""
    __tablename__ = "embedding_model_usage"

    model       = Column(String, primary_key=True)
    provider    = Column(String, nullable=False, default="voyage")
    tokens_used = Column(BigInteger, nullable=False, default=0)
    status      = Column(String, nullable=False, default="active")  # active | exhausted | broken
    last_error  = Column(Text, nullable=True)
    updated_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                          onupdate=lambda: datetime.now(timezone.utc))

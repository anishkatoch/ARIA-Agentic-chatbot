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

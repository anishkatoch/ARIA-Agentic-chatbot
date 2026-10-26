from sqlalchemy import (
    Column, String, DateTime, Text, ForeignKey,
    BigInteger, Boolean, Integer, UniqueConstraint, Index
)
from sqlalchemy.orm import declarative_base
from sqlalchemy.dialects.postgresql import UUID, ARRAY
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
    # Which department roles (see UserRole below) may see/query this KB.
    # NULL or empty = unrestricted, visible to every signed-in user - the
    # same behavior every existing knowledge base had before roles existed.
    allowed_roles = Column(ARRAY(String), nullable=True)
    created_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                          onupdate=lambda: datetime.now(timezone.utc))


class KnowledgeBaseDocument(Base):
    """A single file bundled into a KnowledgeBase. May have multiple
    versions over time - see `version`/`is_superseded`/`previous_version_id`
    below. `status` keeps its original pending/complete/failed pipeline
    meaning and is untouched by versioning (see planning/
    PLAN-document-versioning.md "Resolved: status field decision")."""
    __tablename__ = "knowledge_base_documents"

    id                = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    knowledge_base_id = Column(UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=False)
    filename          = Column(String, nullable=False)
    file_size         = Column(BigInteger, nullable=False)
    chunks_stored     = Column(BigInteger, nullable=True)
    status            = Column(String, nullable=False, default="pending")  # pending | complete | failed
    vector_ids        = Column(Text, nullable=True)  # comma-separated vectorstore ids, for targeted delete
    uploaded_at       = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # ── Versioning (see planning/PLAN-document-versioning.md) ──────────────
    item_id             = Column(String, nullable=True, index=True)  # optional client-supplied identity key
    content_hash        = Column(String, nullable=True, index=True)  # SHA256 of raw uploaded bytes
    version             = Column(Integer, nullable=False, default=1)
    previous_version_id = Column(UUID(as_uuid=True), ForeignKey("knowledge_base_documents.id"), nullable=True)
    is_superseded       = Column(Boolean, nullable=False, default=False)
    extracted_text       = Column(Text, nullable=True)  # full parsed text, for diffing only - never embedded

    __table_args__ = (
        Index("ix_kb_documents_knowledge_base_id", "knowledge_base_id"),
        Index(
            "uq_kb_documents_item_id_active", "knowledge_base_id", "item_id",
            unique=True,
            postgresql_where=(is_superseded.is_(False)) & (item_id.isnot(None)),
        ),
        Index(
            "uq_kb_documents_filename_active", "knowledge_base_id", "filename",
            unique=True,
            postgresql_where=(is_superseded.is_(False)) & (item_id.is_(None)),
        ),
    )


class SessionKnowledgeBase(Base):
    """Which knowledge bases are currently attached to a chat session - the
    server-side record of the attach/detach panel's state, so it survives a
    page refresh (or a different browser/device hitting the same session_id)
    instead of living only in frontend localStorage. A session's own uploads
    need no row here - they're already looked up by session_id directly."""
    __tablename__ = "session_knowledge_bases"

    session_id        = Column(UUID(as_uuid=True), ForeignKey("sessions.id"), primary_key=True)
    knowledge_base_id = Column(UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), primary_key=True)
    attached_at       = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_session_kbs_session_id", "session_id"),
    )


class UserRole(Base):
    """Department-access roles for one signed-in user (e.g. "hr", "it",
    "finance", "sales") - used to restrict which knowledge bases they can
    see/query. There is no local `users` table (accounts live entirely in
    Supabase Auth), so this is keyed by email, looked up server-side from
    the verified token's email claim - never trusted from the token/client
    itself, since this table is the actual source of authorization truth.
    A user with no row here (or an empty roles list) can still see every
    unrestricted knowledge base, just none of the role-restricted ones."""
    __tablename__ = "user_roles"

    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email      = Column(String, nullable=False, unique=True, index=True)
    roles      = Column(ARRAY(String), nullable=False, default=list)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                         onupdate=lambda: datetime.now(timezone.utc))


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

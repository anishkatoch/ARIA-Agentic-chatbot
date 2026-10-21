"""SQLAlchemy model(s) for the connectors package.

Uses its OWN declarative_base() rather than importing the host app's shared
Base from app.models.db - this is deliberate so the connectors package has
zero import dependency on the host app's model module. SQLAlchemy supports
multiple independent declarative bases living in the same process; the only
thing that would NOT be safe is a single `Base.metadata.create_all()` call
expecting this table to be included in some other Base's merged schema.

That is not how this project manages schema: migrations are hand-written
under alembic/versions/ (see e.g. 003_connector_tokens.py, which calls
op.create_table(...) directly with explicit sa.Column(...) definitions - it
does not autogenerate from Base.metadata), and alembic/env.py only points at
app.models.db.Base for the host app's own tables. This module's table is
created independently by its own migration, so having a separate Base here
does not break or fragment anything. A host app integrating this package
only needs to make sure the connector_tokens table exists (run the
alembic migration, or create_all() this module's own Base.metadata directly
if not using Alembic) - it never needs to merge this Base into its own.
"""
from datetime import datetime, timezone
import uuid

from sqlalchemy import Column, String, DateTime, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class ConnectorToken(Base):
    """Per-user OAuth token storage for connectors that need user-scoped auth
    (Gmail, Google Drive, Salesforce). Replaces CONNECTOR_POC's flat JSON token
    cache files (.gmail_token.json etc.), which had no concept of "which user"."""
    __tablename__ = "connector_tokens"

    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id       = Column(String, nullable=False, index=True)  # NOT UUID - guest ids look like "guest-<email>", Supabase ids are UUID strings; store as plain String to fit both
    provider      = Column(String, nullable=False)  # "gmail" | "google_drive" | "salesforce"
    access_token  = Column(Text, nullable=False)
    refresh_token = Column(Text, nullable=True)
    expires_at    = Column(DateTime, nullable=True)
    extra         = Column(Text, nullable=True)  # JSON-encoded provider-specific metadata (e.g. Salesforce instance_url)
    created_at    = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at    = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                            onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_user_provider"),
    )


class ConnectorCredential(Base):
    """Per-user override of an API-key-style connector's credentials (GitHub
    PAT, Jira base_url+email+token, ServiceNow instance+username+password,
    Azure DevOps org_url+PAT). These 4 connectors otherwise fall back to a
    single shared app-level env var - a row here lets one user "bring their
    own key" that takes priority for their own requests only, without
    affecting any other user or requiring an env var / server restart.

    Distinct from ConnectorToken (OAuth access/refresh tokens for Gmail/
    Google Drive/Salesforce, which are obtained via a login redirect, not
    typed in by hand) - `credentials` is a single JSON blob since each of
    these 4 providers' credential shape is different."""
    __tablename__ = "connector_credentials"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id     = Column(String, nullable=False, index=True)
    provider    = Column(String, nullable=False)  # "github" | "jira" | "servicenow" | "azure_devops"
    credentials = Column(Text, nullable=False)     # JSON-encoded, shape varies by provider
    created_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                          onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_cred_user_provider"),
    )

"""Add connector_tokens table - per-user OAuth token storage for the
Gmail / Google Drive / Salesforce connectors (CONNECTOR_POC integration).

Revision ID: 003
Revises: 002
Create Date: 2026-10-19

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = '003'
down_revision = '002'
branch_labels = None
depends_on = None


def _has_table(conn, table: str) -> bool:
    insp = inspect(conn)
    return table in insp.get_table_names()


def upgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, "connector_tokens"):
        return
    op.create_table(
        "connector_tokens",
        sa.Column("id",            postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id",       sa.String(), nullable=False),
        sa.Column("provider",      sa.String(), nullable=False),
        sa.Column("access_token",  sa.Text(),   nullable=False),
        sa.Column("refresh_token", sa.Text(),   nullable=True),
        sa.Column("expires_at",    sa.DateTime(), nullable=True),
        sa.Column("extra",         sa.Text(),   nullable=True),
        sa.Column("created_at",    sa.DateTime(), nullable=True),
        sa.Column("updated_at",    sa.DateTime(), nullable=True),
    )
    op.create_index("ix_connector_tokens_user_id", "connector_tokens", ["user_id"])
    op.create_unique_constraint(
        "uq_user_provider", "connector_tokens", ["user_id", "provider"]
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, "connector_tokens"):
        return
    op.drop_table("connector_tokens")

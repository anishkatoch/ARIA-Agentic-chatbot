"""Add connector_credentials table - per-user "bring your own key" override
for the GitHub / Jira / ServiceNow / Azure DevOps connectors, which otherwise
only read a single shared app-level env var.

Revision ID: 004
Revises: 003
Create Date: 2026-10-21

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = '004'
down_revision = '003'
branch_labels = None
depends_on = None


def _has_table(conn, table: str) -> bool:
    insp = inspect(conn)
    return table in insp.get_table_names()


def upgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, "connector_credentials"):
        return
    op.create_table(
        "connector_credentials",
        sa.Column("id",          postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id",     sa.String(), nullable=False),
        sa.Column("provider",    sa.String(), nullable=False),
        sa.Column("credentials", sa.Text(),   nullable=False),
        sa.Column("created_at",  sa.DateTime(), nullable=True),
        sa.Column("updated_at",  sa.DateTime(), nullable=True),
    )
    op.create_index("ix_connector_credentials_user_id", "connector_credentials", ["user_id"])
    op.create_unique_constraint(
        "uq_cred_user_provider", "connector_credentials", ["user_id", "provider"]
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, "connector_credentials"):
        return
    op.drop_table("connector_credentials")

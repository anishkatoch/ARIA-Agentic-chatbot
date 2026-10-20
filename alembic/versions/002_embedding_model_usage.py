"""Add embedding_model_usage table - tracks per-model token usage for
automatic fallback when a model's free quota is exhausted or it fails.

Revision ID: 002
Revises: 001
Create Date: 2026-10-18

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = '002'
down_revision = '001'
branch_labels = None
depends_on = None


def _has_table(conn, table: str) -> bool:
    insp = inspect(conn)
    return table in insp.get_table_names()


def upgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, "embedding_model_usage"):
        return
    op.create_table(
        "embedding_model_usage",
        sa.Column("model",       sa.String(),     primary_key=True),
        sa.Column("provider",    sa.String(),     nullable=False, server_default="voyage"),
        sa.Column("tokens_used", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status",      sa.String(),     nullable=False, server_default="active"),
        sa.Column("last_error",  sa.Text(),        nullable=True),
        sa.Column("updated_at",  sa.DateTime(),    nullable=True),
    )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, "embedding_model_usage"):
        op.drop_table("embedding_model_usage")

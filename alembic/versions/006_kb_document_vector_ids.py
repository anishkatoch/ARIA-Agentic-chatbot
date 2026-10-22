"""Add vector_ids to knowledge_base_documents - lets admin per-file delete
remove exactly that file's vectors from the KB's vector store instead of
only deleting the bookkeeping row.

Revision ID: 006
Revises: 005
Create Date: 2026-10-21

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = '006'
down_revision = '005'
branch_labels = None
depends_on = None


def _has_column(conn, table: str, column: str) -> bool:
    insp = inspect(conn)
    return column in [c["name"] for c in insp.get_columns(table)]


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn, "knowledge_base_documents", "vector_ids"):
        op.add_column("knowledge_base_documents", sa.Column("vector_ids", sa.Text(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn, "knowledge_base_documents", "vector_ids"):
        op.drop_column("knowledge_base_documents", "vector_ids")

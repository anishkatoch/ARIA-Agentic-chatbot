"""Add knowledge_bases + knowledge_base_documents tables - admin-curated,
shared document sets that any logged-in user can point their chat at.

Revision ID: 005
Revises: 004
Create Date: 2026-10-21

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = '005'
down_revision = '004'
branch_labels = None
depends_on = None


def _has_table(conn, table: str) -> bool:
    insp = inspect(conn)
    return table in insp.get_table_names()


def upgrade() -> None:
    conn = op.get_bind()

    if not _has_table(conn, "knowledge_bases"):
        op.create_table(
            "knowledge_bases",
            sa.Column("id",          postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("name",        sa.String(), nullable=False),
            sa.Column("description", sa.Text(),   nullable=True),
            sa.Column("session_id",  sa.String(), nullable=False),
            sa.Column("status",      sa.String(), nullable=False, server_default="active"),
            sa.Column("created_by",  sa.String(), nullable=True),
            sa.Column("created_at",  sa.DateTime(), nullable=True),
            sa.Column("updated_at",  sa.DateTime(), nullable=True),
        )
        op.create_unique_constraint("uq_knowledge_bases_name", "knowledge_bases", ["name"])
        op.create_unique_constraint("uq_knowledge_bases_session_id", "knowledge_bases", ["session_id"])

    if not _has_table(conn, "knowledge_base_documents"):
        op.create_table(
            "knowledge_base_documents",
            sa.Column("id",                postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("knowledge_base_id",  postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("knowledge_bases.id"), nullable=False),
            sa.Column("filename",     sa.String(),     nullable=False),
            sa.Column("file_size",    sa.BigInteger(),  nullable=False),
            sa.Column("chunks_stored", sa.BigInteger(), nullable=True),
            sa.Column("status",       sa.String(), nullable=False, server_default="pending"),
            sa.Column("uploaded_at",  sa.DateTime(), nullable=True),
        )
        op.create_index(
            "ix_kb_documents_knowledge_base_id", "knowledge_base_documents", ["knowledge_base_id"]
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, "knowledge_base_documents"):
        op.drop_table("knowledge_base_documents")
    if _has_table(conn, "knowledge_bases"):
        op.drop_table("knowledge_bases")

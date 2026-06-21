"""Phase 5 — add dedup columns to documents table

Revision ID: 001
Revises:
Create Date: 2026-06-20

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = '001'
down_revision = None
branch_labels = None
depends_on = None


def _has_column(conn, table: str, column: str) -> bool:
    insp = inspect(conn)
    cols = [c["name"] for c in insp.get_columns(table)]
    return column in cols


def _has_table(conn, table: str) -> bool:
    insp = inspect(conn)
    return table in insp.get_table_names()


def upgrade() -> None:
    conn = op.get_bind()

    # ── Create tables that may not exist yet ─────────────────────
    if not _has_table(conn, "sessions"):
        op.create_table(
            "sessions",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )

    if not _has_table(conn, "documents"):
        op.create_table(
            "documents",
            sa.Column("id",             postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("session_id",     postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("sessions.id"), nullable=False),
            sa.Column("client_token",   sa.String(),   nullable=False, server_default="anonymous"),
            sa.Column("filename",       sa.String(),   nullable=False),
            sa.Column("file_size",      sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("content_hash",   sa.String(),   nullable=False, server_default=""),
            sa.Column("first_chunk",    sa.Text(),     nullable=True),
            sa.Column("middle_chunk",   sa.Text(),     nullable=True),
            sa.Column("last_chunk",     sa.Text(),     nullable=True),
            sa.Column("avg_confidence", sa.String(),   nullable=True),
            sa.Column("chunks_stored",  sa.BigInteger(), nullable=True),
            sa.Column("source_type",    sa.String(),   nullable=False, server_default="file"),
            sa.Column("status",         sa.String(),   nullable=False, server_default="complete"),
            sa.Column("uploaded_at",    sa.DateTime(), nullable=True),
        )
        op.create_index("ix_documents_client_token", "documents", ["client_token"])
        op.create_unique_constraint(
            "uq_client_token_content_hash", "documents", ["client_token", "content_hash"]
        )
        return

    # ── Table already exists — add missing columns one by one ────
    new_cols = {
        "client_token":   sa.Column("client_token",   sa.String(),     nullable=False, server_default="anonymous"),
        "file_size":      sa.Column("file_size",      sa.BigInteger(), nullable=False, server_default="0"),
        "content_hash":   sa.Column("content_hash",   sa.String(),     nullable=False, server_default=""),
        "first_chunk":    sa.Column("first_chunk",    sa.Text(),       nullable=True),
        "middle_chunk":   sa.Column("middle_chunk",   sa.Text(),       nullable=True),
        "last_chunk":     sa.Column("last_chunk",     sa.Text(),       nullable=True),
        "avg_confidence": sa.Column("avg_confidence", sa.String(),     nullable=True),
        "chunks_stored":  sa.Column("chunks_stored",  sa.BigInteger(), nullable=True),
        "source_type":    sa.Column("source_type",    sa.String(),     nullable=False, server_default="file"),
        "status":         sa.Column("status",         sa.String(),     nullable=False, server_default="complete"),
        "uploaded_at":    sa.Column("uploaded_at",    sa.DateTime(),   nullable=True),
    }

    for col_name, col_def in new_cols.items():
        if not _has_column(conn, "documents", col_name):
            op.add_column("documents", col_def)

    # ── Add index if missing ──────────────────────────────────────
    existing_indexes = {i["name"] for i in inspect(conn).get_indexes("documents")}
    if "ix_documents_client_token" not in existing_indexes:
        op.create_index("ix_documents_client_token", "documents", ["client_token"])

    # ── Add unique constraint if missing ─────────────────────────
    existing_uqs = {u["name"] for u in inspect(conn).get_unique_constraints("documents")}
    if "uq_client_token_content_hash" not in existing_uqs:
        op.create_unique_constraint(
            "uq_client_token_content_hash", "documents", ["client_token", "content_hash"]
        )


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, "documents"):
        return

    existing_uqs = {u["name"] for u in inspect(conn).get_unique_constraints("documents")}
    if "uq_client_token_content_hash" in existing_uqs:
        op.drop_constraint("uq_client_token_content_hash", "documents", type_="unique")

    existing_indexes = {i["name"] for i in inspect(conn).get_indexes("documents")}
    if "ix_documents_client_token" in existing_indexes:
        op.drop_index("ix_documents_client_token", table_name="documents")

    for col_name in ["client_token", "file_size", "content_hash", "first_chunk",
                     "middle_chunk", "last_chunk", "avg_confidence",
                     "chunks_stored", "source_type", "status", "uploaded_at"]:
        if _has_column(conn, "documents", col_name):
            op.drop_column("documents", col_name)

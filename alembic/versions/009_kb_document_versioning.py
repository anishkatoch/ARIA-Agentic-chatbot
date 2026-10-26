"""Add versioning columns to knowledge_base_documents - item_id,
content_hash, version, previous_version_id, is_superseded, extracted_text.
See planning/PLAN-document-versioning.md.

Revision ID: 009
Revises: 008
Create Date: 2026-10-25

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import inspect

revision = '009'
down_revision = '008'
branch_labels = None
depends_on = None

_TABLE = "knowledge_base_documents"


def _has_column(conn, table: str, column: str) -> bool:
    return any(c["name"] == column for c in inspect(conn).get_columns(table))


def _has_index(conn, table: str, index: str) -> bool:
    return any(i["name"] == index for i in inspect(conn).get_indexes(table))


def upgrade() -> None:
    conn = op.get_bind()

    if not _has_column(conn, _TABLE, "item_id"):
        op.add_column(_TABLE, sa.Column("item_id", sa.String(), nullable=True))
    if not _has_column(conn, _TABLE, "content_hash"):
        op.add_column(_TABLE, sa.Column("content_hash", sa.String(), nullable=True))
    if not _has_column(conn, _TABLE, "version"):
        op.add_column(_TABLE, sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    if not _has_column(conn, _TABLE, "previous_version_id"):
        op.add_column(
            _TABLE,
            sa.Column("previous_version_id", UUID(as_uuid=True),
                      sa.ForeignKey(f"{_TABLE}.id"), nullable=True),
        )
    if not _has_column(conn, _TABLE, "is_superseded"):
        op.add_column(_TABLE, sa.Column("is_superseded", sa.Boolean(), nullable=False, server_default="false"))
    if not _has_column(conn, _TABLE, "extracted_text"):
        op.add_column(_TABLE, sa.Column("extracted_text", sa.Text(), nullable=True))

    if not _has_index(conn, _TABLE, "ix_knowledge_base_documents_item_id"):
        op.create_index("ix_knowledge_base_documents_item_id", _TABLE, ["item_id"])
    if not _has_index(conn, _TABLE, "ix_knowledge_base_documents_content_hash"):
        op.create_index("ix_knowledge_base_documents_content_hash", _TABLE, ["content_hash"])

    # Pre-existing rows had no versioning concept - any filename already
    # uploaded more than once in the same KB (no unique constraint existed
    # before this migration) would violate the new partial unique index
    # below. Keep only the most recently uploaded row per (kb_id, filename)
    # as is_superseded=false; mark any older duplicates superseded so the
    # index can be created. This is a one-time cleanup of pre-existing data,
    # not a version chain (previous_version_id is left NULL for these).
    conn.execute(sa.text(f"""
        UPDATE {_TABLE} AS d
        SET is_superseded = true
        WHERE d.item_id IS NULL
          AND d.is_superseded = false
          AND EXISTS (
              SELECT 1 FROM {_TABLE} AS newer
              WHERE newer.knowledge_base_id = d.knowledge_base_id
                AND newer.filename = d.filename
                AND newer.item_id IS NULL
                AND newer.is_superseded = false
                AND (newer.uploaded_at, newer.id) > (d.uploaded_at, d.id)
          )
    """))

    if not _has_index(conn, _TABLE, "uq_kb_documents_item_id_active"):
        op.create_index(
            "uq_kb_documents_item_id_active", _TABLE, ["knowledge_base_id", "item_id"],
            unique=True,
            postgresql_where=sa.text("is_superseded = false AND item_id IS NOT NULL"),
        )
    if not _has_index(conn, _TABLE, "uq_kb_documents_filename_active"):
        op.create_index(
            "uq_kb_documents_filename_active", _TABLE, ["knowledge_base_id", "filename"],
            unique=True,
            postgresql_where=sa.text("is_superseded = false AND item_id IS NULL"),
        )


def downgrade() -> None:
    conn = op.get_bind()
    for idx in (
        "uq_kb_documents_filename_active",
        "uq_kb_documents_item_id_active",
        "ix_knowledge_base_documents_content_hash",
        "ix_knowledge_base_documents_item_id",
    ):
        if _has_index(conn, _TABLE, idx):
            op.drop_index(idx, table_name=_TABLE)
    for col in (
        "extracted_text", "is_superseded", "previous_version_id",
        "version", "content_hash", "item_id",
    ):
        if _has_column(conn, _TABLE, col):
            op.drop_column(_TABLE, col)

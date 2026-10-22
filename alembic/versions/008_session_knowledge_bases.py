"""Add session_knowledge_bases table - which knowledge bases are attached to
a chat session, server-side (survives page refresh / a different browser or
device on the same session_id, unlike the old frontend-only selection).

Revision ID: 008
Revises: 007
Create Date: 2026-10-23

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import inspect

revision = '008'
down_revision = '007'
branch_labels = None
depends_on = None


def _has_table(conn, table: str) -> bool:
    return inspect(conn).has_table(table)


def upgrade() -> None:
    conn = op.get_bind()

    if not _has_table(conn, "session_knowledge_bases"):
        op.create_table(
            "session_knowledge_bases",
            sa.Column("session_id", UUID(as_uuid=True),
                      sa.ForeignKey("sessions.id"), primary_key=True),
            sa.Column("knowledge_base_id", UUID(as_uuid=True),
                      sa.ForeignKey("knowledge_bases.id"), primary_key=True),
            sa.Column("attached_at", sa.DateTime(), nullable=True),
        )
        op.create_index(
            "ix_session_kbs_session_id", "session_knowledge_bases", ["session_id"]
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, "session_knowledge_bases"):
        op.drop_index("ix_session_kbs_session_id", table_name="session_knowledge_bases")
        op.drop_table("session_knowledge_bases")

"""Add user_roles table and knowledge_bases.allowed_roles - role-based access
control for knowledge bases. A user's department roles (e.g. "hr", "it",
"finance", "sales") are assigned by the admin and stored locally (there is no
local `users` table - accounts live in Supabase Auth - so this is keyed by
email, looked up at request time from the verified token's email claim).
A knowledge base with an empty/null allowed_roles list is unrestricted
(visible to everyone), matching every existing knowledge base's current
behavior - this migration changes no existing visibility.

Revision ID: 007
Revises: 006
Create Date: 2026-10-22

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from sqlalchemy import inspect

revision = '007'
down_revision = '006'
branch_labels = None
depends_on = None


def _has_table(conn, table: str) -> bool:
    return inspect(conn).has_table(table)


def _has_column(conn, table: str, column: str) -> bool:
    insp = inspect(conn)
    return column in [c["name"] for c in insp.get_columns(table)]


def upgrade() -> None:
    conn = op.get_bind()

    if not _has_table(conn, "user_roles"):
        op.create_table(
            "user_roles",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("email", sa.String(), nullable=False, unique=True),
            sa.Column("roles", ARRAY(sa.String()), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_user_roles_email", "user_roles", ["email"], unique=True)

    if not _has_column(conn, "knowledge_bases", "allowed_roles"):
        op.add_column(
            "knowledge_bases",
            sa.Column("allowed_roles", ARRAY(sa.String()), nullable=True),
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn, "knowledge_bases", "allowed_roles"):
        op.drop_column("knowledge_bases", "allowed_roles")
    if _has_table(conn, "user_roles"):
        op.drop_index("ix_user_roles_email", table_name="user_roles")
        op.drop_table("user_roles")

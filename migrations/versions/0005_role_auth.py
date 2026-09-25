"""Add role accounts and revocable, expiring login sessions.

Revision ID: 0005_role_auth
Revises: 0004_public_semantic_cache
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0005_role_auth"
down_revision: str | Sequence[str] | None = "0004_public_semantic_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_users",
        sa.Column(
            "id",
            pg.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("role", sa.String(16), nullable=False, unique=True),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "role IN ('ANALYST', 'MANAGER', 'AUDITOR', 'PLATFORM')", name="ck_auth_users_role"
        ),
        sa.CheckConstraint(
            "email = lower(email) AND length(email) > 3", name="ck_auth_users_email"
        ),
        sa.CheckConstraint("failed_attempts >= 0", name="ck_auth_users_failed_attempts"),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("secret_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "user_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("auth_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="ck_auth_sessions_token_hash"),
        sa.CheckConstraint(
            "secret_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_auth_sessions_secret_fingerprint"
        ),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])
    op.execute("GRANT SELECT ON TABLE public.auth_users TO invoiceops_app")
    op.execute(
        "GRANT UPDATE (failed_attempts, locked_until) ON TABLE public.auth_users TO invoiceops_app"
    )
    op.execute("GRANT SELECT, INSERT, DELETE ON TABLE public.auth_sessions TO invoiceops_app")


def downgrade() -> None:
    op.execute("REVOKE ALL ON TABLE public.auth_users, public.auth_sessions FROM invoiceops_app")
    op.drop_index("ix_auth_sessions_expires_at", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_table("auth_users")

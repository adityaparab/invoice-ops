"""Store bounded public-data semantic cache entries separately from audit history.

Revision ID: 0004_public_semantic_cache
Revises: 0003_embedding_model_version
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004_public_semantic_cache"
down_revision: str | Sequence[str] | None = "0003_embedding_model_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "gateway_semantic_cache",
        sa.Column("cache_key", sa.String(64), primary_key=True),
        sa.Column("namespace", sa.String(64), nullable=False),
        sa.Column("embedding", Vector(), nullable=False),
        sa.Column("response_json", JSONB(), nullable=False),
        sa.Column("model", sa.String(160), nullable=False),
        sa.Column("model_version", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_gateway_semantic_cache_namespace_expiry",
        "gateway_semantic_cache",
        ["namespace", "expires_at"],
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.gateway_semantic_cache "
        "TO invoiceops_app"
    )


def downgrade() -> None:
    op.execute("REVOKE ALL ON TABLE public.gateway_semantic_cache FROM invoiceops_app")
    op.drop_index("ix_gateway_semantic_cache_namespace_expiry", table_name="gateway_semantic_cache")
    op.drop_table("gateway_semantic_cache")

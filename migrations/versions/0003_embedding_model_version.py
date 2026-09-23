"""Pin the model route used to populate each invoice embedding.

Revision ID: 0003_embedding_model_version
Revises: 0002_append_only_audit
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_embedding_model_version"
down_revision: str | Sequence[str] | None = "0002_append_only_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("invoices", sa.Column("embedding_model_version", sa.String(160)))
    op.execute(
        "UPDATE invoices SET embedding_model_version = '__legacy_unpinned__' "
        "WHERE embedding IS NOT NULL"
    )
    op.create_check_constraint(
        "ck_invoices_embedding_model_pair",
        "invoices",
        "(embedding IS NULL) = (embedding_model_version IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_invoices_embedding_model_pair", "invoices", type_="check")
    op.drop_column("invoices", "embedding_model_version")

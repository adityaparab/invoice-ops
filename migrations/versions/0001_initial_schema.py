"""Create the operational schema and audit record structures.

Revision ID: 0001_initial_schema
Revises: None
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql as pg

revision: str = "0001_initial_schema"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _id() -> sa.Column[UUID]:
    return sa.Column(
        "id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )


def _created_at() -> sa.Column[datetime]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _version_columns() -> list[sa.Column[str]]:
    return [
        sa.Column(f"{component}_version", sa.String(128), nullable=False)
        for component in ("graph", "model", "prompt", "policy")
    ]


def _nonblank(table: str, *columns: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        " AND ".join(f"length(btrim({column})) > 0" for column in columns),
        name=f"ck_{table}_nonblank",
    )


def _json_type(table: str, column: str, kind: str = "object") -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"jsonb_typeof({column}) = '{kind}'", name=f"ck_{table}_{column}_type"
    )


def _run_invoice_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["run_id", "invoice_id"], ["runs.id", "runs.invoice_id"], name=f"fk_{table}_run_invoice"
    )


def _create_erp_tables() -> None:
    op.create_table(
        "vendors",
        _id(),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("tax_id", sa.String(128)),
        sa.Column("bank_account_iban", sa.String(34)),
        sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"),
        _created_at(),
        sa.UniqueConstraint("external_id", name="uq_vendors_external_id"),
        sa.CheckConstraint("status IN ('ACTIVE', 'INACTIVE')", name="ck_vendors_status"),
        _nonblank("vendors", "external_id", "name"),
    )
    op.create_table(
        "purchase_orders",
        _id(),
        sa.Column("vendor_id", pg.UUID(as_uuid=True), sa.ForeignKey("vendors.id"), nullable=False),
        sa.Column("po_number", sa.String(128), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="OPEN"),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("issued_on", sa.Date(), nullable=False),
        sa.Column("total_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("lines", pg.JSONB(), nullable=False),
        _created_at(),
        sa.UniqueConstraint("po_number", name="uq_purchase_orders_po_number"),
        sa.CheckConstraint(
            "status IN ('OPEN', 'PARTIALLY_RECEIVED', 'CLOSED', 'CANCELLED')",
            name="ck_purchase_orders_status",
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_purchase_orders_currency"),
        sa.CheckConstraint(
            "total_amount <> 'NaN'::numeric", name="ck_purchase_orders_amount_finite"
        ),
        _nonblank("purchase_orders", "po_number"),
        _json_type("purchase_orders", "lines", "array"),
    )
    op.create_index("ix_purchase_orders_vendor_status", "purchase_orders", ["vendor_id", "status"])
    op.create_table(
        "goods_receipts",
        _id(),
        sa.Column(
            "purchase_order_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("purchase_orders.id"),
            nullable=False,
        ),
        sa.Column("receipt_number", sa.String(128), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lines", pg.JSONB(), nullable=False),
        _created_at(),
        sa.UniqueConstraint("receipt_number", name="uq_goods_receipts_receipt_number"),
        _nonblank("goods_receipts", "receipt_number"),
        _json_type("goods_receipts", "lines", "array"),
    )
    op.create_index(
        "ix_goods_receipts_po_received", "goods_receipts", ["purchase_order_id", "received_at"]
    )


def _create_invoice_tables() -> None:
    op.create_table(
        "invoices",
        _id(),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("raw_ref", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(64), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="RECEIVED"),
        sa.Column("vendor_id", pg.UUID(as_uuid=True), sa.ForeignKey("vendors.id")),
        sa.Column("purchase_order_id", pg.UUID(as_uuid=True), sa.ForeignKey("purchase_orders.id")),
        sa.Column("invoice_number", sa.String(128)),
        sa.Column("po_number", sa.String(128)),
        sa.Column("issued_on", sa.Date()),
        sa.Column("due_on", sa.Date()),
        sa.Column("currency", sa.String(3)),
        sa.Column("subtotal", sa.Numeric(18, 2)),
        sa.Column("tax_amount", sa.Numeric(18, 2)),
        sa.Column("total_amount", sa.Numeric(18, 2)),
        sa.Column("extraction", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("extraction_confidence", sa.Numeric(9, 6)),
        sa.Column("embedding", Vector(384)),
        _created_at(),
        sa.UniqueConstraint("content_hash", name="uq_invoices_content_hash"),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_invoices_content_hash"),
        sa.CheckConstraint(
            "content_type IN ('application/pdf', 'image/png', 'image/jpeg')",
            name="ck_invoices_content_type",
        ),
        sa.CheckConstraint("source IN ('UPLOAD', 'EMAIL')", name="ck_invoices_source"),
        sa.CheckConstraint(
            "status IN ('RECEIVED', 'QUEUED', 'PROCESSING', 'NEEDS_REVIEW', 'APPROVED', "
            "'REJECTED', 'RETURNED', 'ARCHIVED', 'FAILED')",
            name="ck_invoices_status",
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_invoices_currency"),
        sa.CheckConstraint("extraction_confidence BETWEEN 0 AND 1", name="ck_invoices_confidence"),
        sa.CheckConstraint(
            "subtotal <> 'NaN'::numeric AND tax_amount <> 'NaN'::numeric "
            "AND total_amount <> 'NaN'::numeric",
            name="ck_invoices_amounts_finite",
        ),
        _nonblank("invoices", "raw_ref"),
        _json_type("invoices", "extraction"),
    )
    op.create_index("ix_invoices_status_created", "invoices", ["status", "created_at"])
    op.create_index("ix_invoices_vendor_id", "invoices", ["vendor_id"])
    op.create_index("ix_invoices_purchase_order_id", "invoices", ["purchase_order_id"])
    op.create_index(
        "ix_invoices_embedding_hnsw",
        "invoices",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_table(
        "invoice_lines",
        _id(),
        sa.Column(
            "invoice_id", pg.UUID(as_uuid=True), sa.ForeignKey("invoices.id"), nullable=False
        ),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("sku", sa.String(128)),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column("unit_price", sa.Numeric(18, 2), nullable=False),
        sa.Column("tax_rate", sa.Numeric(9, 6), nullable=False),
        sa.Column("tax_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("line_total", sa.Numeric(18, 2), nullable=False),
        _created_at(),
        sa.UniqueConstraint("invoice_id", "line_number", name="uq_invoice_lines_invoice_line"),
        sa.CheckConstraint("line_number > 0", name="ck_invoice_lines_line_number"),
        sa.CheckConstraint(
            "quantity <> 'NaN'::numeric AND unit_price <> 'NaN'::numeric "
            "AND tax_rate <> 'NaN'::numeric AND tax_amount <> 'NaN'::numeric "
            "AND line_total <> 'NaN'::numeric",
            name="ck_invoice_lines_amounts_finite",
        ),
    )


def _create_workflow_tables() -> None:
    op.create_table(
        "runs",
        _id(),
        sa.Column(
            "invoice_id", pg.UUID(as_uuid=True), sa.ForeignKey("invoices.id"), nullable=False
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="QUEUED"),
        sa.Column("graph_version", sa.String(128), nullable=False),
        sa.Column("trace_id", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error", pg.JSONB()),
        _created_at(),
        sa.UniqueConstraint("id", "invoice_id", name="uq_runs_id_invoice"),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'PAUSED', 'COMPLETED', 'FAILED', 'CANCELLED')",
            name="ck_runs_status",
        ),
        sa.CheckConstraint("completed_at >= started_at", name="ck_runs_time_order"),
        _nonblank("runs", "graph_version", "trace_id"),
        _json_type("runs", "error"),
    )
    op.create_index("ix_runs_invoice_started", "runs", ["invoice_id", "started_at"])
    op.create_index("ix_runs_status_created", "runs", ["status", "created_at"])
    op.create_table(
        "checkpoints",
        _id(),
        sa.Column("run_id", pg.UUID(as_uuid=True), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("node", sa.String(128), nullable=False),
        sa.Column("graph_version", sa.String(128), nullable=False),
        sa.Column("state", pg.JSONB(), nullable=False),
        _created_at(),
        sa.UniqueConstraint("run_id", "sequence", name="uq_checkpoints_run_sequence"),
        sa.CheckConstraint("sequence > 0", name="ck_checkpoints_sequence"),
        _nonblank("checkpoints", "node", "graph_version"),
        _json_type("checkpoints", "state"),
    )
    op.create_table(
        "ingestion_requests",
        sa.Column("idempotency_key", sa.String(128), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("invoice_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("response_status", sa.SmallInteger(), nullable=False),
        sa.Column("response_body", pg.JSONB(), nullable=False),
        _created_at(),
        _run_invoice_fk("ingestion_requests"),
        sa.CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="ck_ingestion_requests_hash"),
        sa.CheckConstraint(
            "response_status IN (200, 201)", name="ck_ingestion_requests_response_status"
        ),
        _nonblank("ingestion_requests", "idempotency_key"),
        _json_type("ingestion_requests", "response_body"),
    )
    op.create_index(
        "ix_ingestion_requests_run_invoice", "ingestion_requests", ["run_id", "invoice_id"]
    )
    op.create_table(
        "webhook_nonces",
        sa.Column("nonce", sa.String(128), primary_key=True),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=False),
        _created_at(),
        _nonblank("webhook_nonces", "nonce"),
    )
    op.create_index("ix_webhook_nonces_signed_at", "webhook_nonces", ["signed_at"])


def _create_audit_tables() -> None:
    op.create_table(
        "ledger",
        _id(),
        sa.Column("run_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("invoice_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("node", sa.String(128)),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        *_version_columns(),
        sa.Column("payload", pg.JSONB(), nullable=False),
        sa.Column("supersedes_id", pg.UUID(as_uuid=True), sa.ForeignKey("ledger.id")),
        _created_at(),
        _run_invoice_fk("ledger"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_ledger_run_sequence"),
        sa.CheckConstraint("sequence > 0", name="ck_ledger_sequence"),
        sa.CheckConstraint(
            "actor_type IN ('SYSTEM', 'AGENT', 'HUMAN', 'POLICY')", name="ck_ledger_actor_type"
        ),
        _nonblank(
            "ledger",
            "event_type",
            "actor_id",
            "graph_version",
            "model_version",
            "prompt_version",
            "policy_version",
        ),
        _json_type("ledger", "payload"),
    )
    op.create_index("ix_ledger_invoice_created_id", "ledger", ["invoice_id", "created_at", "id"])
    op.create_index("ix_ledger_supersedes_id", "ledger", ["supersedes_id"])
    op.create_table(
        "exceptions",
        _id(),
        sa.Column("run_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("invoice_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("exception_type", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="OPEN"),
        sa.Column("priority", sa.SmallInteger(), nullable=False),
        sa.Column("sla_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("assigned_to", sa.String(128)),
        sa.Column("evidence", pg.JSONB(), nullable=False),
        sa.Column("recommendation", pg.JSONB(), nullable=False),
        _created_at(),
        _run_invoice_fk("exceptions"),
        sa.UniqueConstraint("id", "run_id", "invoice_id", name="uq_exceptions_id_run_invoice"),
        sa.CheckConstraint(
            "status IN ('OPEN', 'IN_REVIEW', 'RESOLVED', 'ESCALATED')", name="ck_exceptions_status"
        ),
        sa.CheckConstraint("priority BETWEEN 0 AND 3", name="ck_exceptions_priority"),
        _nonblank("exceptions", "exception_type"),
        _json_type("exceptions", "evidence"),
        _json_type("exceptions", "recommendation"),
    )
    op.create_index("ix_exceptions_status_sla", "exceptions", ["status", "sla_due_at"])
    op.create_index("ix_exceptions_run_invoice", "exceptions", ["run_id", "invoice_id"])
    op.create_index("ix_exceptions_invoice_id", "exceptions", ["invoice_id"])
    op.create_table(
        "decisions",
        _id(),
        sa.Column("run_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("invoice_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("exception_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        *_version_columns(),
        sa.Column("supersedes_id", pg.UUID(as_uuid=True), sa.ForeignKey("decisions.id")),
        _created_at(),
        _run_invoice_fk("decisions"),
        sa.ForeignKeyConstraint(
            ["exception_id", "run_id", "invoice_id"],
            ["exceptions.id", "exceptions.run_id", "exceptions.invoice_id"],
            name="fk_decisions_exception_run_invoice",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_decisions_idempotency_key"),
        sa.CheckConstraint(
            "action IN ('APPROVE', 'RETURN', 'ESCALATE', 'REJECT')", name="ck_decisions_action"
        ),
        sa.CheckConstraint("actor_type = 'HUMAN'", name="ck_decisions_actor_type"),
        _nonblank(
            "decisions",
            "idempotency_key",
            "rationale",
            "reason_code",
            "actor_id",
            "graph_version",
            "model_version",
            "prompt_version",
            "policy_version",
        ),
    )
    op.create_index("ix_decisions_run_invoice", "decisions", ["run_id", "invoice_id"])
    op.create_index("ix_decisions_exception_id", "decisions", ["exception_id"])
    op.create_index(
        "ix_decisions_invoice_created_id", "decisions", ["invoice_id", "created_at", "id"]
    )
    op.create_index("ix_decisions_supersedes_id", "decisions", ["supersedes_id"])


def upgrade() -> None:
    op.execute("SET LOCAL search_path = public")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    _create_erp_tables()
    _create_invoice_tables()
    _create_workflow_tables()
    _create_audit_tables()


def downgrade() -> None:
    op.execute("SET LOCAL search_path = public")
    for table in (
        "decisions",
        "exceptions",
        "ledger",
        "webhook_nonces",
        "ingestion_requests",
        "checkpoints",
        "runs",
        "invoice_lines",
        "invoices",
        "goods_receipts",
        "purchase_orders",
        "vendors",
    ):
        op.drop_table(table)
    # The extension may predate this migration or serve another schema.

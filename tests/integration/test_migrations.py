"""Validate schema behavior and reversibility against a real pgvector database."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import psycopg
import pytest
from tests.integration.support import (
    EXCEPTION_ID,
    INVOICE_ID,
    RUN_ID,
    insert_decision,
    insert_ledger,
    migrate,
    seed_invoice_and_run,
)

pytestmark = pytest.mark.integration


def test_migration_cli_supports_upgrade_downgrade_and_reupgrade(
    migrated_database: psycopg.Connection[tuple[object, ...]],
) -> None:
    expected_tables = {
        "alembic_version",
        "vendors",
        "purchase_orders",
        "goods_receipts",
        "invoices",
        "invoice_lines",
        "runs",
        "checkpoints",
        "ingestion_requests",
        "webhook_nonces",
        "ledger",
        "exceptions",
        "decisions",
        "gateway_semantic_cache",
        "auth_users",
        "auth_sessions",
    }
    tables = migrated_database.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    ).fetchall()
    assert {row[0] for row in tables} == expected_tables

    seed_invoice_and_run(migrated_database)
    insert_ledger(migrated_database)
    migrate("downgrade", "base")
    assert migrated_database.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    ).fetchall() == [("alembic_version",)]
    assert migrated_database.execute("SELECT * FROM alembic_version").fetchall() == []

    migrate("upgrade", "head")
    assert migrated_database.execute("SELECT count(*) FROM invoices").fetchone() == (0,)
    assert migrated_database.execute("SELECT count(*) FROM ledger").fetchone() == (0,)


def test_decimal_values_and_timezone_aware_instants_round_trip_without_float_loss(
    migrated_database: psycopg.Connection[tuple[object, ...]],
) -> None:
    seed_invoice_and_run(migrated_database)
    amount = Decimal("9999999999999999.99")
    quantity = Decimal("12345678901234.5678")
    rate = Decimal("0.123456")
    migrated_database.execute(
        "UPDATE invoices SET total_amount = %s, created_at = %s WHERE id = %s",
        (amount, datetime.fromisoformat("2026-09-23T10:00:00+02:00"), INVOICE_ID),
    )
    migrated_database.execute(
        "INSERT INTO invoice_lines (invoice_id, line_number, description, quantity, unit_price, "
        "tax_rate, tax_amount, line_total) "
        "VALUES (%s, 1, 'Synthetic component', %s, %s, %s, %s, %s)",
        (INVOICE_ID, quantity, Decimal("0.01"), rate, Decimal("1.23"), Decimal("12.34")),
    )
    assert migrated_database.execute(
        "SELECT total_amount, created_at FROM invoices WHERE id = %s", (INVOICE_ID,)
    ).fetchone() == (amount, datetime(2026, 9, 23, 8, tzinfo=UTC))
    assert migrated_database.execute(
        "SELECT quantity, unit_price, tax_rate FROM invoice_lines WHERE invoice_id = %s",
        (INVOICE_ID,),
    ).fetchone() == (quantity, Decimal("0.01"), rate)
    with pytest.raises(psycopg.errors.NumericValueOutOfRange):
        migrated_database.execute(
            "UPDATE invoices SET total_amount = %s WHERE id = %s",
            (Decimal("10000000000000000.00"), INVOICE_ID),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        migrated_database.execute(
            "UPDATE invoices SET total_amount = %s WHERE id = %s", (Decimal("NaN"), INVOICE_ID)
        )


def test_embeddings_enforce_384_dimensions_and_use_hnsw_cosine_index(
    migrated_database: psycopg.Connection[tuple[object, ...]],
) -> None:
    seed_invoice_and_run(migrated_database)
    embedding = "[1," + ",".join("0" for _ in range(383)) + "]"
    migrated_database.execute(
        "UPDATE invoices SET embedding = %s::vector, embedding_model_version = %s WHERE id = %s",
        (embedding, "synthetic-embed@v1", INVOICE_ID),
    )
    query = "SELECT id FROM invoices ORDER BY embedding <=> %s::vector LIMIT 1"
    assert migrated_database.execute(query, (embedding,)).fetchone() == (INVOICE_ID,)
    with migrated_database.transaction():
        migrated_database.execute("SET LOCAL enable_seqscan = off")
        plan = migrated_database.execute("EXPLAIN " + query, (embedding,)).fetchall()
        assert "ix_invoices_embedding_hnsw" in str(plan)
    index = migrated_database.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_invoices_embedding_hnsw'"
    ).fetchone()
    assert index is not None
    assert "USING hnsw (embedding vector_cosine_ops)" in str(index[0])
    with pytest.raises(psycopg.errors.DataException):
        migrated_database.execute(
            "UPDATE invoices SET embedding = '[1,2,3]'::vector WHERE id = %s", (INVOICE_ID,)
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        migrated_database.execute(
            "UPDATE invoices SET embedding_model_version = NULL WHERE id = %s", (INVOICE_ID,)
        )


def test_migration_preserves_legacy_vectors_without_comparing_them_to_new_models(
    migrated_database: psycopg.Connection[tuple[object, ...]],
) -> None:
    migrate("downgrade", "0002_append_only_audit")
    seed_invoice_and_run(migrated_database)
    vector = "[1," + ",".join("0" for _ in range(383)) + "]"
    migrated_database.execute(
        "UPDATE invoices SET embedding = %s::vector WHERE id = %s", (vector, INVOICE_ID)
    )
    migrate("upgrade", "head")
    assert migrated_database.execute(
        "SELECT embedding_model_version FROM invoices WHERE id = %s", (INVOICE_ID,)
    ).fetchone() == ("__legacy_unpinned__",)


def test_uniqueness_prevents_duplicate_content_checkpoints_and_replay_claims(
    migrated_database: psycopg.Connection[tuple[object, ...]],
) -> None:
    seed_invoice_and_run(migrated_database)
    statements: list[tuple[str, tuple[object, ...]]] = [
        (
            "INSERT INTO invoices (content_hash, raw_ref, content_type, source) "
            "VALUES (%s, 'duplicate', 'application/pdf', 'EMAIL')",
            ("b" * 64,),
        ),
        (
            "INSERT INTO vendors (external_id, name) "
            "VALUES ('synthetic-vendor-1', 'Fixture Vendor')",
            (),
        ),
        (
            "INSERT INTO checkpoints (run_id, sequence, node, graph_version, state) "
            "VALUES (%s, 1, 'Ingest', 'graph@v1', '{}')",
            (RUN_ID,),
        ),
        (
            "INSERT INTO ingestion_requests (idempotency_key, request_hash, invoice_id, run_id, "
            "response_status, response_body) VALUES ('synthetic-upload-1', %s, %s, %s, 201, '{}')",
            ("c" * 64, INVOICE_ID, RUN_ID),
        ),
        (
            "INSERT INTO webhook_nonces (nonce, signed_at) "
            "VALUES ('synthetic-nonce-1', '2026-09-23T08:00:00Z')",
            (),
        ),
        (
            "INSERT INTO invoice_lines (invoice_id, line_number, description, quantity, "
            "unit_price, tax_rate, tax_amount, line_total) "
            "VALUES (%s, 1, 'Fixture', 1, 1, 0, 0, 1)",
            (INVOICE_ID,),
        ),
    ]
    for statement, parameters in statements:
        migrated_database.execute(statement, parameters)
        with pytest.raises(psycopg.errors.UniqueViolation):
            migrated_database.execute(statement, parameters)


def test_audit_constraints_require_valid_actors_versions_and_related_invoice(
    migrated_database: psycopg.Connection[tuple[object, ...]],
) -> None:
    seed_invoice_and_run(migrated_database)
    insert_ledger(migrated_database)
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert_ledger(migrated_database)
    with pytest.raises(psycopg.errors.NotNullViolation):
        insert_ledger(migrated_database, sequence=2, model_version=None)
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_ledger(migrated_database, sequence=2, model_version="  ")
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_ledger(migrated_database, sequence=2, actor_type="UNKNOWN")
    migrated_database.execute(
        "INSERT INTO invoices (id, content_hash, raw_ref, content_type, source) "
        "VALUES (%s, %s, 'another-fixture', 'image/png', 'EMAIL')",
        (UUID(int=99), "d" * 64),
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        insert_ledger(migrated_database, sequence=2, invoice_id=UUID(int=99))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        migrated_database.execute("DELETE FROM invoices WHERE id = %s", (INVOICE_ID,))

    migrated_database.execute(
        "INSERT INTO exceptions (id, run_id, invoice_id, exception_type, priority, sla_due_at, "
        "evidence, recommendation) VALUES (%s, %s, %s, 'PRICE_MISMATCH', 1, "
        "'2026-09-24T08:00:00Z', '{}', '{}')",
        (EXCEPTION_ID, RUN_ID, INVOICE_ID),
    )
    insert_decision(migrated_database)
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert_decision(migrated_database)
    with pytest.raises(psycopg.errors.NotNullViolation):
        insert_decision(migrated_database, key="second", policy_version=None)
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_decision(migrated_database, key="second", action="PAY")


def test_operational_constraints_reject_invalid_statuses_and_json_shapes(
    migrated_database: psycopg.Connection[tuple[object, ...]],
) -> None:
    seed_invoice_and_run(migrated_database)
    statements = [
        "UPDATE invoices SET status = 'PAID'",
        "UPDATE invoices SET currency = 'usd'",
        "UPDATE invoices SET content_hash = 'invalid'",
        "UPDATE invoices SET source = 'UNKNOWN'",
        "UPDATE invoices SET extraction = '[]'::jsonb",
        "UPDATE invoices SET extraction_confidence = 1.000001",
        "UPDATE runs SET status = 'UNKNOWN'",
        "UPDATE runs SET graph_version = ''",
        "UPDATE runs SET error = '[]'::jsonb",
    ]
    for statement in statements:
        with pytest.raises(psycopg.errors.CheckViolation):
            migrated_database.execute(statement)


def test_erp_headers_require_unique_numbers_valid_references_and_line_arrays(
    migrated_database: psycopg.Connection[tuple[object, ...]],
) -> None:
    vendor = migrated_database.execute(
        "INSERT INTO vendors (external_id, name) "
        "VALUES ('synthetic-vendor-2', 'Synthetic Components') RETURNING id"
    ).fetchone()
    assert vendor is not None
    po_statement = (
        "INSERT INTO purchase_orders "
        "(vendor_id, po_number, currency, issued_on, total_amount, lines) "
        "VALUES (%s, 'PO-SYNTHETIC-1', 'USD', '2026-09-23', 12.34, '[]') RETURNING id"
    )
    purchase_order = migrated_database.execute(po_statement, (vendor[0],)).fetchone()
    assert purchase_order is not None
    with pytest.raises(psycopg.errors.UniqueViolation):
        migrated_database.execute(po_statement, (vendor[0],))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        migrated_database.execute("UPDATE purchase_orders SET vendor_id = %s", (UUID(int=999),))
    receipt_statement = (
        "INSERT INTO goods_receipts (purchase_order_id, receipt_number, received_at, lines) "
        "VALUES (%s, 'RECEIPT-SYNTHETIC-1', '2026-09-23T08:00:00Z', '[]')"
    )
    migrated_database.execute(receipt_statement, (purchase_order[0],))
    with pytest.raises(psycopg.errors.UniqueViolation):
        migrated_database.execute(receipt_statement, (purchase_order[0],))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        migrated_database.execute("DELETE FROM purchase_orders WHERE id = %s", (purchase_order[0],))
    for statement in (
        "UPDATE vendors SET status = 'UNKNOWN'",
        "UPDATE purchase_orders SET status = 'UNKNOWN'",
        "UPDATE purchase_orders SET lines = '{}'::jsonb",
        "UPDATE goods_receipts SET lines = '{}'::jsonb",
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            migrated_database.execute(statement)

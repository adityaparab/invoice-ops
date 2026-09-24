"""Atomic, idempotent persistence of a versioned synthetic ERP fixture."""

import hashlib
from typing import Literal

import psycopg
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from invoiceops_agent.schemas.erp import (
    ERPFixture,
    GoldenERPSeed,
    GoodsReceipt,
    PurchaseOrder,
    Vendor,
)

SeedOutcome = Literal["created", "unchanged"]
_LOCK = int.from_bytes(hashlib.sha256(b"invoiceops:erp-seed@v1").digest()[:8], signed=True)


class ERPSeedConflict(Exception):
    """Existing seed rows differ from the versioned fixture or are incomplete."""


async def _existing_count[T: Vendor | PurchaseOrder | GoodsReceipt](
    connection: psycopg.AsyncConnection[DictRow],
    *,
    table: Literal["vendors", "purchase_orders", "goods_receipts"],
    columns: str,
    expected: tuple[T, ...],
    model: type[T],
) -> int:
    if not expected:
        return 0
    cursor = await connection.execute(
        f"SELECT {columns} FROM public.{table} WHERE id = ANY(%s)",
        ([record.id for record in expected],),
    )
    rows = {row["id"]: row for row in await cursor.fetchall()}
    for record in expected:
        row = rows.get(record.id)
        if row is not None and model.model_validate(row) != record:
            raise ERPSeedConflict(f"Existing {table} row differs from the synthetic fixture")
    return len(rows)


async def seed_fixture(
    connection: psycopg.AsyncConnection[DictRow], fixture: ERPFixture
) -> SeedOutcome:
    """Insert all records once; exact reruns are no-ops and drift fails closed."""
    await connection.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK,))
    counts = await _fixture_counts(connection, fixture)
    expected_counts = (
        len(fixture.vendors),
        len(fixture.purchase_orders),
        len(fixture.goods_receipts),
    )
    if counts == expected_counts:
        return "unchanged"
    if any(counts):
        raise ERPSeedConflict("Existing synthetic ERP fixture is incomplete")
    await _insert_fixture(connection, fixture, include_vendors=True)
    return "created"


async def _fixture_counts(
    connection: psycopg.AsyncConnection[DictRow], fixture: ERPFixture | GoldenERPSeed
) -> tuple[int, int, int]:
    return (
        await _existing_count(
            connection,
            table="vendors",
            columns="id, external_id, name, tax_id, bank_account_iban, status, created_at",
            expected=fixture.vendors,
            model=Vendor,
        ),
        await _existing_count(
            connection,
            table="purchase_orders",
            columns=(
                "id, vendor_id, po_number, status, currency, issued_on, "
                "total_amount, lines, created_at"
            ),
            expected=fixture.purchase_orders,
            model=PurchaseOrder,
        ),
        await _existing_count(
            connection,
            table="goods_receipts",
            columns="id, purchase_order_id, receipt_number, received_at, lines, created_at",
            expected=fixture.goods_receipts,
            model=GoodsReceipt,
        ),
    )


async def seed_golden_fixture(
    connection: psycopg.AsyncConnection[DictRow], fixture: GoldenERPSeed
) -> SeedOutcome:
    """Add the golden orders beside the base fixture, or verify an exact rerun."""
    if fixture.version != "golden-erp@v1":
        raise ERPSeedConflict("Expected the golden ERP fixture version")
    await connection.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK,))
    counts = await _fixture_counts(connection, fixture)
    full = (
        len(fixture.vendors),
        len(fixture.purchase_orders),
        len(fixture.goods_receipts),
    )
    if counts == full:
        return "unchanged"
    if counts not in {(0, 0, 0), (len(fixture.vendors), 0, 0)}:
        raise ERPSeedConflict("Existing golden ERP fixture is incomplete")
    await _insert_fixture(connection, fixture, include_vendors=counts[0] == 0)
    return "created"


async def _insert_fixture(
    connection: psycopg.AsyncConnection[DictRow],
    fixture: ERPFixture | GoldenERPSeed,
    *,
    include_vendors: bool,
) -> None:
    if include_vendors:
        for vendor in fixture.vendors:
            await connection.execute(
                "INSERT INTO public.vendors "
                "(id, external_id, name, tax_id, bank_account_iban, status, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    vendor.id,
                    vendor.external_id,
                    vendor.name,
                    vendor.tax_id,
                    vendor.bank_account_iban,
                    vendor.status,
                    vendor.created_at,
                ),
            )
    for order in fixture.purchase_orders:
        await connection.execute(
            "INSERT INTO public.purchase_orders "
            "(id, vendor_id, po_number, status, currency, issued_on, "
            "total_amount, lines, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                order.id,
                order.vendor_id,
                order.po_number,
                order.status,
                order.currency,
                order.issued_on,
                order.total_amount,
                Jsonb([line.model_dump(mode="json") for line in order.lines]),
                order.created_at,
            ),
        )
    for receipt in fixture.goods_receipts:
        await connection.execute(
            "INSERT INTO public.goods_receipts "
            "(id, purchase_order_id, receipt_number, received_at, lines, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                receipt.id,
                receipt.purchase_order_id,
                receipt.receipt_number,
                receipt.received_at,
                Jsonb([line.model_dump(mode="json") for line in receipt.lines]),
                receipt.created_at,
            ),
        )

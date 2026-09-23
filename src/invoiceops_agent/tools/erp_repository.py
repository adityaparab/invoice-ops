"""Read a bounded, typed ERP snapshot for one purchase order."""

import asyncio
import logging
from time import perf_counter

import psycopg
from psycopg.rows import DictRow
from pydantic import ValidationError

from invoiceops_agent.schemas.erp import GoodsReceipt, PurchaseOrder, Vendor
from invoiceops_agent.schemas.matching import ERPSnapshot

logger = logging.getLogger(__name__)


class ERPDataIntegrityError(Exception):
    """An existing ERP relation cannot be represented by the matching contract."""


def _fields(row: DictRow, prefix: str, columns: tuple[str, ...]) -> dict[str, object]:
    return {column: row[f"{prefix}_{column}"] for column in columns}


class ERPRepository:
    @staticmethod
    async def snapshot(
        connection: psycopg.AsyncConnection[DictRow], po_number: str
    ) -> ERPSnapshot | None:
        started = perf_counter()
        # One SQL statement gives every joined row the same Postgres MVCC snapshot.
        try:
            async with asyncio.timeout(5):
                cursor = await connection.execute(
                    "SELECT p.id AS p_id, p.vendor_id AS p_vendor_id, p.po_number AS p_po_number, "
                    "p.status AS p_status, p.currency AS p_currency, p.issued_on AS p_issued_on, "
                    "p.total_amount AS p_total_amount, p.lines AS p_lines, "
                    "p.created_at AS p_created_at, "
                    "v.id AS v_id, v.external_id AS v_external_id, v.name AS v_name, "
                    "v.tax_id AS v_tax_id, v.bank_account_iban AS v_bank_account_iban, "
                    "v.status AS v_status, v.created_at AS v_created_at, "
                    "r.id AS r_id, r.purchase_order_id AS r_purchase_order_id, "
                    "r.receipt_number AS r_receipt_number, r.received_at AS r_received_at, "
                    "r.lines AS r_lines, r.created_at AS r_created_at "
                    "FROM public.purchase_orders p "
                    "LEFT JOIN public.vendors v ON v.id = p.vendor_id "
                    "LEFT JOIN public.goods_receipts r ON r.purchase_order_id = p.id "
                    "WHERE p.po_number = %s ORDER BY r.received_at, r.id LIMIT 101",
                    (po_number,),
                )
                rows = await cursor.fetchall()
        except (TimeoutError, psycopg.Error) as error:
            logger.error(
                "erp_snapshot_read_failed error_type=%s duration_ms=%.3f",
                type(error).__name__,
                (perf_counter() - started) * 1000,
            )
            raise
        if not rows:
            logger.info(
                "erp_snapshot_read found=false receipts=0 duration_ms=%.3f",
                (perf_counter() - started) * 1000,
            )
            return None
        if rows[0]["v_id"] is None:
            raise ERPDataIntegrityError("Purchase order vendor is missing")
        if len(rows) > 100:
            raise ERPDataIntegrityError("Purchase order has too many goods receipts")
        try:
            snapshot = ERPSnapshot(
                vendor=Vendor.model_validate(
                    _fields(
                        rows[0],
                        "v",
                        (
                            "id",
                            "external_id",
                            "name",
                            "tax_id",
                            "bank_account_iban",
                            "status",
                            "created_at",
                        ),
                    )
                ),
                purchase_order=PurchaseOrder.model_validate(
                    _fields(
                        rows[0],
                        "p",
                        (
                            "id",
                            "vendor_id",
                            "po_number",
                            "status",
                            "currency",
                            "issued_on",
                            "total_amount",
                            "lines",
                            "created_at",
                        ),
                    )
                ),
                goods_receipts=tuple(
                    GoodsReceipt.model_validate(
                        _fields(
                            row,
                            "r",
                            (
                                "id",
                                "purchase_order_id",
                                "receipt_number",
                                "received_at",
                                "lines",
                                "created_at",
                            ),
                        )
                    )
                    for row in rows
                    if row["r_id"] is not None
                ),
            )
        except ValidationError:
            raise ERPDataIntegrityError("ERP snapshot violates the matching contract") from None
        logger.info(
            "erp_snapshot_read found=true receipts=%d duration_ms=%.3f",
            len(snapshot.goods_receipts),
            (perf_counter() - started) * 1000,
        )
        return snapshot

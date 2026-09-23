"""Synthetic typed three-way inputs shared by unit and integration tests."""

from decimal import Decimal
from typing import Literal
from uuid import UUID

from invoiceops_agent.schemas.erp import ERPFixture, PurchaseOrder
from invoiceops_agent.schemas.extraction import InvoiceExtraction
from invoiceops_agent.schemas.matching import ERPSnapshot, MatchRequest
from invoiceops_agent.tools.erp_generator import generate_fixture


def snapshot_for(
    status: Literal["OPEN", "PARTIALLY_RECEIVED", "CLOSED", "CANCELLED"] = "CLOSED",
    *,
    fixture: ERPFixture | None = None,
) -> ERPSnapshot:
    source = fixture if fixture is not None else generate_fixture()
    order = next(order for order in source.purchase_orders if order.status == status)
    vendor = next(vendor for vendor in source.vendors if vendor.id == order.vendor_id)
    receipts = tuple(
        receipt for receipt in source.goods_receipts if receipt.purchase_order_id == order.id
    )
    return ERPSnapshot(vendor=vendor, purchase_order=order, goods_receipts=receipts)


def _invoice_lines(
    order: PurchaseOrder, snapshot: ERPSnapshot, *, received_only: bool
) -> tuple[list[dict[str, object]], Decimal]:
    received = {
        line.line_number: line.received_quantity
        for receipt in snapshot.goods_receipts
        for line in receipt.lines
    }
    lines: list[dict[str, object]] = []
    subtotal = Decimal(0)
    for line in order.lines:
        quantity = received[line.line_number] if received_only else line.quantity
        subtotal += quantity * line.unit_price
        lines.append(
            {
                "description": {"value": line.description, "confidence": "1"},
                "quantity": {"value": str(quantity), "confidence": "1"},
                "unit_price": {"value": str(line.unit_price), "confidence": "1"},
                "tax_rate": {"value": "0", "confidence": "1"},
                "line_total": {"value": str(quantity * line.unit_price), "confidence": "1"},
            }
        )
    return lines, subtotal


def matching_request(
    snapshot: ERPSnapshot | None = None, *, received_only: bool = False
) -> MatchRequest:
    source = snapshot if snapshot is not None else snapshot_for()
    order = source.purchase_order
    lines, subtotal = _invoice_lines(order, source, received_only=received_only)
    extraction = InvoiceExtraction.model_validate(
        {
            "vendor_name": {"value": source.vendor.name, "confidence": "1"},
            "vendor_tax_id": {"value": source.vendor.tax_id, "confidence": "1"},
            "bank_account_iban": {"value": source.vendor.bank_account_iban, "confidence": "1"},
            "invoice_number": {"value": "SYN-MATCH-001", "confidence": "1"},
            "po_number": {"value": order.po_number, "confidence": "1"},
            "currency": {"value": order.currency, "confidence": "1"},
            "invoice_date": {"value": "2026-08-27", "confidence": "1"},
            "due_date": {"value": None, "confidence": "0"},
            "subtotal": {"value": str(subtotal), "confidence": "1"},
            "tax_amount": {"value": "0", "confidence": "1"},
            "total_amount": {"value": str(subtotal), "confidence": "1"},
            "line_items": lines,
        }
    )
    return MatchRequest(
        run_id=UUID(int=1),
        invoice_id=UUID(int=2),
        trace_id="a" * 32,
        extraction=extraction,
        snapshot=snapshot,
    )

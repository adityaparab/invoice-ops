"""Pure three-way tolerances and missing-data states have exact evidence."""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError
from tests.unit.matching_support import matching_request, snapshot_for

from invoiceops_agent.schemas.erp import PurchaseOrderLine
from invoiceops_agent.schemas.matching import ERPSnapshot, MatchConfig
from invoiceops_agent.tools.matching import match_invoice

pytestmark = pytest.mark.unit


def test_fully_received_invoice_matches_and_reproduces_fingerprints() -> None:
    snapshot = snapshot_for()
    request = matching_request(snapshot)
    first = match_invoice(request)
    second = match_invoice(request)
    assert first == second
    assert first.status == "PASS"
    assert first.snapshot_found and first.po_status == "CLOSED"
    assert all(check.status == "MATCH" for check in first.identity_checks)
    assert all(check.status == "MATCH" for check in first.numeric_checks)
    assert len(first.numeric_checks) == 3 + 5 * len(snapshot.purchase_order.lines)


def test_invoice_subtotal_is_bounded_by_order_and_received_value() -> None:
    request = matching_request(snapshot_for())
    current = request.extraction.subtotal
    assert current.value is not None
    raised = current.model_copy(update={"value": current.value + Decimal("1000")})
    extraction = request.extraction.model_copy(update={"subtotal": raised})
    result = match_invoice(request.model_copy(update={"extraction": extraction}))
    assert result.status == "FAIL"
    checks = {check.field: check for check in result.numeric_checks if check.line_number is None}
    assert checks["subtotal_ordered"].status == "MISMATCH"
    assert checks["subtotal_received"].status == "MISMATCH"


def test_unit_price_boundary_and_signed_delta() -> None:
    request = matching_request(snapshot_for())
    extraction = request.extraction
    first = extraction.line_items[0]
    original = first.unit_price.value
    assert original is not None
    policy = MatchConfig(price_relative_tolerance=Decimal(0))
    for change, expected_status in ((Decimal("0.01"), "MATCH"), (Decimal("0.02"), "MISMATCH")):
        changed_line = first.model_copy(
            update={"unit_price": first.unit_price.model_copy(update={"value": original + change})}
        )
        changed = extraction.model_copy(
            update={"line_items": (changed_line, *extraction.line_items[1:])}
        )
        result = match_invoice(request.model_copy(update={"extraction": changed}), policy)
        price = next(check for check in result.numeric_checks if check.field == "unit_price")
        assert price.difference == change
        assert price.tolerance == Decimal("0.01")
        assert price.status == expected_status
        assert result.status == ("PASS" if expected_status == "MATCH" else "FAIL")


def test_partial_receipt_caps_invoice_quantity_without_requiring_full_po_quantity() -> None:
    snapshot = snapshot_for("PARTIALLY_RECEIVED")
    matched = match_invoice(matching_request(snapshot, received_only=True))
    assert matched.status == "PASS"
    overbilled = match_invoice(matching_request(snapshot))
    assert overbilled.status == "FAIL"
    shortfalls = [
        check
        for check in overbilled.numeric_checks
        if check.field == "quantity_received" and check.status == "MISMATCH"
    ]
    assert shortfalls and all(
        check.difference is not None and check.difference > 0 for check in shortfalls
    )
    assert all(check.rule == "at_most" for check in shortfalls)


def test_multiple_receipts_are_aggregated_by_po_line() -> None:
    snapshot = snapshot_for("PARTIALLY_RECEIVED")
    first = snapshot.goods_receipts[0]
    remainder = tuple(
        received_line.model_copy(
            update={"received_quantity": ordered.quantity - received_line.received_quantity}
        )
        for ordered, received_line in zip(snapshot.purchase_order.lines, first.lines, strict=True)
    )
    second = first.model_copy(
        update={
            "id": UUID(int=999),
            "receipt_number": "SYN-SECOND-RECEIPT",
            "received_at": first.received_at + timedelta(days=1),
            "lines": remainder,
        }
    )
    combined = ERPSnapshot(
        vendor=snapshot.vendor,
        purchase_order=snapshot.purchase_order,
        goods_receipts=(first, second),
    )
    result = match_invoice(matching_request(combined))
    assert result.status == "PASS"
    assert all(
        check.status == "MATCH"
        for check in result.numeric_checks
        if check.field == "quantity_received"
    )


def test_missing_snapshot_and_unknown_value_are_incomplete() -> None:
    request = matching_request()
    assert match_invoice(request).status == "INCOMPLETE"
    assert not match_invoice(request).snapshot_found
    snapshot = snapshot_for()
    request = matching_request(snapshot)
    first = request.extraction.line_items[0]
    unknown = first.unit_price.model_copy(update={"value": None, "confidence": Decimal(0)})
    changed_line = first.model_copy(update={"unit_price": unknown})
    extraction = request.extraction.model_copy(
        update={"line_items": (changed_line, *request.extraction.line_items[1:])}
    )
    result = match_invoice(request.model_copy(update={"extraction": extraction}))
    assert result.status == "INCOMPLETE"
    assert any(check.status == "UNKNOWN" for check in result.numeric_checks)


def test_identity_mismatch_fails_and_config_change_changes_fingerprint() -> None:
    request = matching_request(snapshot_for())
    wrong_vendor = request.extraction.vendor_name.model_copy(
        update={"value": "Other synthetic vendor"}
    )
    extraction = request.extraction.model_copy(update={"vendor_name": wrong_vendor})
    changed = match_invoice(request.model_copy(update={"extraction": extraction}))
    assert changed.status == "FAIL"
    assert changed.identity_checks[1].field == "vendor_name"
    assert changed.identity_checks[1].status == "MISMATCH"
    other_policy = MatchConfig(price_absolute_tolerance=Decimal("0.02"))
    assert (
        match_invoice(request, other_policy).config_sha256 != match_invoice(request).config_sha256
    )


def test_float_tolerance_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MatchConfig.model_validate({"price_absolute_tolerance": 0.01})
    line = snapshot_for().purchase_order.lines[0].model_dump(mode="json")
    line["unit_price"] = 0.01
    with pytest.raises(ValidationError):
        PurchaseOrderLine.model_validate(line)


def test_snapshot_rejects_wrong_receipt_po_and_line_order() -> None:
    snapshot = snapshot_for()
    receipt = snapshot.goods_receipts[0]
    with pytest.raises(ValidationError, match="another purchase order"):
        ERPSnapshot(
            vendor=snapshot.vendor,
            purchase_order=snapshot.purchase_order,
            goods_receipts=(receipt.model_copy(update={"purchase_order_id": UUID(int=999)}),),
        )
    with pytest.raises(ValidationError, match="ordered contiguously"):
        ERPSnapshot(
            vendor=snapshot.vendor,
            purchase_order=snapshot.purchase_order.model_copy(
                update={"lines": tuple(reversed(snapshot.purchase_order.lines))}
            ),
            goods_receipts=snapshot.goods_receipts,
        )

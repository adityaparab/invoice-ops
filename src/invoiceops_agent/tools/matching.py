"""Pure three-way invoice, purchase order, and goods receipt comparisons."""

import unicodedata
from decimal import Decimal, localcontext
from typing import Literal

from invoiceops_agent.schemas.common import model_digest
from invoiceops_agent.schemas.matching import (
    ERPSnapshot,
    IdentityCheck,
    MatchConfig,
    MatchRequest,
    MatchResult,
    NumericCheck,
)
from invoiceops_agent.tools.decimal_math import exact_context


def _text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _identity(
    field: Literal["po_number", "vendor_name", "currency", "line_count", "description"],
    expected: str,
    actual: str | None,
    *,
    line_number: int | None = None,
) -> IdentityCheck:
    status = (
        "UNKNOWN"
        if actual is None or not actual.strip()
        else "MATCH"
        if _text(expected) == _text(actual)
        else "MISMATCH"
    )
    return IdentityCheck(
        field=field, line_number=line_number, expected=expected, actual=actual, status=status
    )


def _numeric(
    field: Literal[
        "unit_price",
        "quantity_ordered",
        "quantity_received",
        "receipt_vs_order",
        "line_total",
        "subtotal_ordered",
        "subtotal_received",
        "receipt_vs_order_total",
    ],
    line_number: int | None,
    rule: Literal["equal", "at_most"],
    expected: Decimal,
    actual: Decimal | None,
    absolute_tolerance: Decimal,
    relative_tolerance: Decimal = Decimal(0),
    *,
    positive_actual: bool = False,
) -> NumericCheck:
    if actual is None:
        return NumericCheck(
            field=field,
            line_number=line_number,
            rule=rule,
            expected=expected,
            actual=None,
            difference=None,
            tolerance=None,
            status="UNKNOWN",
        )
    tolerance = max(absolute_tolerance, abs(expected) * relative_tolerance)
    difference = actual - expected
    invalid_sign = actual < 0 or positive_actual and actual == 0
    matches = (
        abs(difference) <= tolerance if rule == "equal" else difference <= tolerance
    ) and not invalid_sign
    return NumericCheck(
        field=field,
        line_number=line_number,
        rule=rule,
        expected=expected,
        actual=actual,
        difference=difference,
        tolerance=tolerance,
        status="MATCH" if matches else "MISMATCH",
    )


def _received(snapshot: ERPSnapshot) -> dict[int, Decimal]:
    totals = {line.line_number: Decimal(0) for line in snapshot.purchase_order.lines}
    for receipt in snapshot.goods_receipts:
        for line in receipt.lines:
            totals[line.line_number] += line.received_quantity
    return totals


def match_invoice(request: MatchRequest, config: MatchConfig | None = None) -> MatchResult:
    """Compare the supplied immutable inputs with no I/O, clock, or model calls."""
    policy = config if config is not None else MatchConfig()
    snapshot = request.snapshot
    if snapshot is None:
        return MatchResult(
            status="INCOMPLETE",
            snapshot_found=False,
            po_number=request.extraction.po_number.value,
            po_status=None,
            extraction_sha256=model_digest(request.extraction),
            snapshot_sha256=None,
            config_sha256=model_digest(policy),
            config=policy,
            identity_checks=(),
            numeric_checks=(),
        )
    with localcontext(exact_context()):
        identity: list[IdentityCheck] = [
            _identity(
                "po_number", snapshot.purchase_order.po_number, request.extraction.po_number.value
            ),
            _identity("vendor_name", snapshot.vendor.name, request.extraction.vendor_name.value),
            _identity(
                "currency", snapshot.purchase_order.currency, request.extraction.currency.value
            ),
            _identity(
                "line_count",
                str(len(snapshot.purchase_order.lines)),
                str(len(request.extraction.line_items)),
            ),
        ]
        numeric: list[NumericCheck] = []
        received = _received(snapshot)
        received_value = sum(
            (
                received[line.line_number] * line.unit_price
                for line in snapshot.purchase_order.lines
            ),
            Decimal(0),
        )
        numeric.extend(
            (
                _numeric(
                    "subtotal_ordered",
                    None,
                    "at_most",
                    snapshot.purchase_order.total_amount,
                    request.extraction.subtotal.value,
                    policy.amount_absolute_tolerance,
                    policy.amount_relative_tolerance,
                ),
                _numeric(
                    "subtotal_received",
                    None,
                    "at_most",
                    received_value,
                    request.extraction.subtotal.value,
                    policy.amount_absolute_tolerance,
                    policy.amount_relative_tolerance,
                ),
                _numeric(
                    "receipt_vs_order_total",
                    None,
                    "at_most",
                    snapshot.purchase_order.total_amount,
                    received_value,
                    policy.amount_absolute_tolerance,
                    policy.amount_relative_tolerance,
                ),
            )
        )
        for line in snapshot.purchase_order.lines:
            number = line.line_number
            invoice_line = (
                request.extraction.line_items[number - 1]
                if number <= len(request.extraction.line_items)
                else None
            )
            quantity = invoice_line.quantity.value if invoice_line is not None else None
            identity.append(
                _identity(
                    "description",
                    line.description,
                    invoice_line.description.value if invoice_line is not None else None,
                    line_number=number,
                )
            )
            numeric.extend(
                (
                    _numeric(
                        "unit_price",
                        number,
                        "equal",
                        line.unit_price,
                        invoice_line.unit_price.value if invoice_line is not None else None,
                        policy.price_absolute_tolerance,
                        policy.price_relative_tolerance,
                    ),
                    _numeric(
                        "quantity_ordered",
                        number,
                        "at_most",
                        line.quantity,
                        quantity,
                        policy.quantity_absolute_tolerance,
                        positive_actual=True,
                    ),
                    _numeric(
                        "quantity_received",
                        number,
                        "at_most",
                        received[number],
                        quantity,
                        policy.quantity_absolute_tolerance,
                        positive_actual=True,
                    ),
                    _numeric(
                        "receipt_vs_order",
                        number,
                        "at_most",
                        line.quantity,
                        received[number],
                        policy.quantity_absolute_tolerance,
                    ),
                )
            )
            if quantity is None:
                numeric.append(
                    NumericCheck(
                        field="line_total",
                        line_number=number,
                        rule="equal",
                        expected=None,
                        actual=invoice_line.line_total.value if invoice_line is not None else None,
                        difference=None,
                        tolerance=None,
                        status="UNKNOWN",
                    )
                )
            else:
                numeric.append(
                    _numeric(
                        "line_total",
                        number,
                        "equal",
                        quantity * line.unit_price,
                        invoice_line.line_total.value if invoice_line is not None else None,
                        policy.line_total_absolute_tolerance,
                        policy.line_total_relative_tolerance,
                    )
                )
    statuses = [check.status for check in identity] + [check.status for check in numeric]
    status: Literal["PASS", "FAIL", "INCOMPLETE"] = (
        "FAIL" if "MISMATCH" in statuses else "INCOMPLETE" if "UNKNOWN" in statuses else "PASS"
    )
    return MatchResult(
        status=status,
        snapshot_found=True,
        po_number=snapshot.purchase_order.po_number,
        po_status=snapshot.purchase_order.status,
        extraction_sha256=model_digest(request.extraction),
        snapshot_sha256=model_digest(snapshot),
        config_sha256=model_digest(policy),
        config=policy,
        identity_checks=tuple(identity),
        numeric_checks=tuple(numeric),
    )

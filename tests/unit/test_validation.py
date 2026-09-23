"""Offline decision boundaries, missing evidence, and Decimal-context independence."""

from decimal import ROUND_DOWN, Decimal, DefaultContext, Inexact, Rounded, localcontext

import pytest
from pydantic import ValidationError

from invoiceops_agent.schemas.extraction import InvoiceExtraction
from invoiceops_agent.schemas.validation import CurrencyRule, ValidationConfig, ValidationResult
from invoiceops_agent.tools.validation import validate_invoice

pytestmark = pytest.mark.unit


def invoice(**changes: object) -> InvoiceExtraction:
    values: dict[str, object] = {
        "vendor_name": "Synthetic Supplier",
        "vendor_tax_id": None,
        "bank_account_iban": None,
        "invoice_number": "SYNTHETIC-1",
        "po_number": None,
        "currency": "USD",
        "invoice_date": "2026-09-23",
        "due_date": None,
        "subtotal": "20",
        "tax_amount": "4",
        "total_amount": "24",
    }
    values.update({key: value for key, value in changes.items() if key != "line_items"})
    document = {
        key: {"value": value, "confidence": "0" if value is None else "0.5"}
        for key, value in values.items()
    }
    return InvoiceExtraction.model_validate(
        {**document, "line_items": changes.get("line_items", [line()])}
    )


def line(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "description": "Synthetic goods",
        "quantity": "2",
        "unit_price": "10",
        "tax_rate": "0.20",
        "line_total": "20",
        **changes,
    }
    return {
        key: {"value": value, "confidence": "0" if value is None else "0.5"}
        for key, value in values.items()
    }


def strict_usd() -> ValidationConfig:
    return ValidationConfig(
        version="synthetic-strict@v1",
        currencies=(CurrencyRule(currency="USD", decimal_places=2, absolute_tolerance=Decimal(0)),),
    )


def test_valid_invoice_does_not_require_optional_identity_or_po_fields() -> None:
    result = validate_invoice(invoice())
    assert result.status == "PASS" and result.issues == ()
    assert ValidationResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "field",
    [
        "vendor_name",
        "invoice_number",
        "currency",
        "invoice_date",
        "subtotal",
        "tax_amount",
        "total_amount",
    ],
)
def test_missing_header_is_an_issue_not_an_exception(field: str) -> None:
    result = validate_invoice(invoice(**{field: None}))
    assert result.status == "FAIL"
    assert [(issue.code, issue.field) for issue in result.issues] == [("REQUIRED_FIELD", field)]


@pytest.mark.parametrize(
    "field", ["description", "quantity", "unit_price", "tax_rate", "line_total"]
)
def test_missing_line_operand_does_not_invent_a_value_or_an_arithmetic_error(field: str) -> None:
    result = validate_invoice(invoice(line_items=[line(**{field: None})]))
    assert [(issue.code, issue.field, issue.line_number) for issue in result.issues] == [
        ("REQUIRED_FIELD", field, 1)
    ]


def test_missing_tax_rate_never_becomes_a_zero_tax_assumption() -> None:
    result = validate_invoice(
        invoice(tax_amount="0", total_amount="20", line_items=[line(tax_rate=None)])
    )
    assert result.status == "FAIL"
    assert [issue.code for issue in result.issues] == ["REQUIRED_FIELD"]


def test_empty_lines_and_blank_required_text_cannot_pass() -> None:
    result = validate_invoice(invoice(vendor_name=" ", line_items=[]))
    assert [issue.code for issue in result.issues] == ["REQUIRED_FIELD", "EMPTY_LINES"]


@pytest.mark.parametrize("field", ["subtotal", "tax_amount", "total_amount"])
def test_negative_header_amounts_are_outside_regular_invoice_policy(field: str) -> None:
    result = validate_invoice(invoice(**{field: "-1"}))
    assert result.issues[0].code == "NEGATIVE_VALUE" and result.issues[0].field == field


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("quantity", "0", "NONPOSITIVE_QUANTITY"),
        ("quantity", "-1", "NONPOSITIVE_QUANTITY"),
        ("unit_price", "-1", "NEGATIVE_VALUE"),
        ("line_total", "-1", "NEGATIVE_VALUE"),
        ("tax_rate", "-0.01", "TAX_RATE_OUT_OF_RANGE"),
        ("tax_rate", "20", "TAX_RATE_OUT_OF_RANGE"),
    ],
)
def test_line_sign_and_rate_failures_are_typed(field: str, value: str, code: str) -> None:
    result = validate_invoice(invoice(line_items=[line(**{field: value})]))
    assert result.issues[0].code == code
    assert result.issues[0].actual == Decimal(value)
    assert result.issues[0].line_number == 1


@pytest.mark.parametrize(
    "delta,passes", [("0.01", True), ("-0.01", True), ("0.0101", False), ("-0.0101", False)]
)
def test_tolerance_is_absolute_and_inclusive(delta: str, passes: bool) -> None:
    result = validate_invoice(invoice(total_amount=str(Decimal(24) + Decimal(delta))))
    assert (result.status == "PASS") is passes
    if not passes:
        issue = result.issues[0]
        assert issue.code == "TOTAL_MISMATCH"
        assert issue.expected == Decimal(24)
        assert issue.difference == Decimal(delta)
        assert issue.tolerance == Decimal("0.01")


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"line_items": [line(quantity="3")]}, "LINE_MATH_MISMATCH"),
        ({"subtotal": "30"}, "SUBTOTAL_MISMATCH"),
        ({"tax_amount": "5"}, "TAX_MISMATCH"),
        ({"total_amount": "30"}, "TOTAL_MISMATCH"),
    ],
)
def test_each_arithmetic_control_has_independent_evidence(
    changes: dict[str, object], code: str
) -> None:
    result = validate_invoice(invoice(**changes))
    issue = next(issue for issue in result.issues if issue.code == code)
    assert issue.expected is not None and issue.actual is not None
    assert issue.difference == issue.actual - issue.expected
    assert issue.tolerance == Decimal("0.01")


def test_per_line_tax_rounding_is_explicit_and_not_rounding_the_aggregate() -> None:
    lines = [line(quantity="1", unit_price="0.05", line_total="0.05", tax_rate="0.10")] * 2
    good = invoice(subtotal="0.10", tax_amount="0.02", total_amount="0.12", line_items=lines)
    bad = invoice(subtotal="0.10", tax_amount="0.01", total_amount="0.11", line_items=lines)
    assert validate_invoice(good, strict_usd()).status == "PASS"
    result = validate_invoice(bad, strict_usd())
    assert [issue.code for issue in result.issues] == ["TAX_MISMATCH"]
    assert result.issues[0].expected == Decimal("0.02")


@pytest.mark.parametrize(
    "currency,quantity,price,net,tax,total",
    [
        ("JPY", "1", "100.5", "101", "10", "111"),
        ("KWD", "3", "0.1111", "0.333", "0.033", "0.366"),
    ],
)
def test_currency_specific_quantum(
    currency: str, quantity: str, price: str, net: str, tax: str, total: str
) -> None:
    document = invoice(
        currency=currency,
        subtotal=net,
        tax_amount=tax,
        total_amount=total,
        line_items=[line(quantity=quantity, unit_price=price, line_total=net, tax_rate="0.1")],
    )
    assert validate_invoice(document).status == "PASS"


def test_unknown_currency_is_not_silently_assigned_two_decimals() -> None:
    result = validate_invoice(invoice(currency="ZZZ"))
    assert [issue.code for issue in result.issues] == ["UNSUPPORTED_CURRENCY"]


def test_decimal_context_cannot_change_decision_rounding_or_hashes() -> None:
    document = invoice(line_items=[line(unit_price="10.0025")])
    expected = validate_invoice(document)
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_DOWN
        context.Emax = 1
        context.Emin = -1
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        assert validate_invoice(document).model_dump_json() == expected.model_dump_json()


def test_bounded_extraction_extremes_do_not_overflow_arithmetic() -> None:
    document = invoice(
        line_items=[line(quantity="999999999999.999999", unit_price="99999999999999.9999")]
    )
    result = validate_invoice(document)
    assert result.issues[0].code == "LINE_MATH_MISMATCH"
    assert result.issues[0].expected == Decimal("99999999999999999800000000.00")


def test_mutable_default_context_cannot_change_arithmetic(monkeypatch: pytest.MonkeyPatch) -> None:
    document = invoice(line_items=[line(unit_price="10.0025")])
    expected = validate_invoice(document)
    monkeypatch.setattr(DefaultContext, "Emax", 1)
    monkeypatch.setattr(DefaultContext, "Emin", -1)
    monkeypatch.setattr(DefaultContext, "clamp", 1)
    monkeypatch.setitem(DefaultContext.traps, Rounded, True)
    monkeypatch.setitem(DefaultContext.traps, Inexact, True)
    assert validate_invoice(document).model_dump_json() == expected.model_dump_json()


def test_aggregation_uses_every_line_without_ambient_precision_loss() -> None:
    lines = [line()] * 500
    result = validate_invoice(
        invoice(line_items=lines, subtotal="10000", tax_amount="2000", total_amount="12000")
    )
    assert result.status == "PASS"


def test_policy_and_input_pins_change_with_their_contents() -> None:
    default = validate_invoice(invoice())
    different_policy = validate_invoice(invoice(), strict_usd())
    different_input = validate_invoice(invoice(invoice_number="SYNTHETIC-2"))
    assert default.config_sha256 != different_policy.config_sha256
    assert default.input_sha256 != different_input.input_sha256
    assert default.input_sha256 == different_policy.input_sha256
    assert (
        default.model_dump(mode="json")["config"]["currencies"][0]["absolute_tolerance"] == "0.01"
    )


@pytest.mark.parametrize("tolerance", [-1, "NaN", "Infinity", 0.01, True])
def test_policy_rejects_unsafe_tolerances(tolerance: object) -> None:
    with pytest.raises(ValidationError):
        CurrencyRule.model_validate(
            {"currency": "USD", "decimal_places": 2, "absolute_tolerance": tolerance}
        )


def test_policy_is_immutable_and_currencies_are_unique() -> None:
    config = ValidationConfig()
    with pytest.raises(ValidationError):
        config.currencies[0].__setattr__("absolute_tolerance", Decimal(1))
    with pytest.raises(ValidationError):
        ValidationConfig(currencies=(config.currencies[0], config.currencies[0]))


def test_result_rejects_an_inconsistent_decision() -> None:
    result = validate_invoice(invoice())
    with pytest.raises(ValidationError):
        ValidationResult.model_validate({**result.model_dump(), "status": "FAIL"})

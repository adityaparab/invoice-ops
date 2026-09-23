"""Extraction contracts preserve uncertainty and reject malformed unbounded observations."""

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError
from tests.unit.extraction_support import invoice_extraction

from invoiceops_agent.schemas.extraction import InvoiceExtraction

pytestmark = pytest.mark.unit


def test_decimal_and_date_values_roundtrip_as_bounded_canonical_json() -> None:
    invoice = invoice_extraction()
    wire = invoice.model_dump(mode="json")
    assert wire["total_amount"] == {"value": "120", "confidence": "1"}
    assert wire["line_items"][0]["tax_rate"]["value"] == "0.2"
    assert InvoiceExtraction.model_validate_json(json.dumps(wire), strict=True) == invoice
    assert isinstance(invoice.total_amount.value, Decimal)
    assert "Synthetic Supplier" not in repr(invoice)


@pytest.mark.parametrize(
    "value,confidence",
    [
        (None, "0.1"),
        ("20", "1.1"),
        ("20", "-0.1"),
        ("NaN", "1"),
        ("Infinity", "1"),
        ("0.00001", "1"),
        ("100000000000000", "1"),
    ],
)
def test_invalid_decimal_contract_is_rejected(value: object, confidence: str) -> None:
    data = invoice_extraction().model_dump(mode="json")
    data["total_amount"] = {"value": value, "confidence": confidence}
    with pytest.raises(ValidationError):
        InvoiceExtraction.model_validate_json(json.dumps(data), strict=True)


def test_business_inconsistencies_and_unknowns_do_not_trigger_schema_repair() -> None:
    data = invoice_extraction().model_dump(mode="json")
    data["total_amount"]["value"] = "-500"
    data["line_items"][0]["tax_rate"]["value"] = "2"
    data["due_date"]["value"] = "2026-01-01"
    data["due_date"]["confidence"] = "0.3"
    invoice = InvoiceExtraction.model_validate_json(json.dumps(data), strict=True)
    assert invoice.total_amount.value == Decimal("-500")
    assert invoice.line_items[0].tax_rate.value == 2


@pytest.mark.parametrize(
    "change", ["long_text", "too_many_lines", "invalid_date", "extra_field", "missing_field"]
)
def test_bounded_shape_is_required(change: str) -> None:
    data = invoice_extraction().model_dump(mode="json")
    if change == "long_text":
        data["vendor_name"]["value"] = "s" * 257
    elif change == "too_many_lines":
        data["line_items"] = data["line_items"] * 501
    elif change == "invalid_date":
        data["invoice_date"]["value"] = "2026-02-30"
    elif change == "extra_field":
        data["approved"] = True
    else:
        del data["currency"]
    with pytest.raises(ValidationError):
        InvoiceExtraction.model_validate_json(json.dumps(data), strict=True)

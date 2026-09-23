"""Exception classification keeps provenance, ordering, and ambiguity explicit."""

from decimal import Decimal

import pytest
from pydantic import ValidationError
from tests.unit.matching_support import matching_request, snapshot_for

from invoiceops_agent.schemas.exceptions import CODE_ORDER, TaxonomyRequest, TaxonomyResult
from invoiceops_agent.schemas.matching import ERPSnapshot
from invoiceops_agent.tools.exception_taxonomy import classify_exceptions
from invoiceops_agent.tools.matching import match_invoice
from invoiceops_agent.tools.validation import validate_invoice

pytestmark = pytest.mark.unit


def _current_snapshot() -> ERPSnapshot:
    source = snapshot_for("CLOSED")
    return source.model_copy(
        update={"purchase_order": source.purchase_order.model_copy(update={"status": "OPEN"})}
    )


def _request(**changes: object) -> TaxonomyRequest:
    matched = matching_request(_current_snapshot())
    extraction = matched.extraction
    values: dict[str, object] = {
        "run_id": matched.run_id,
        "invoice_id": matched.invoice_id,
        "trace_id": matched.trace_id,
        "extraction": extraction,
        "validation": validate_invoice(extraction),
        "match": match_invoice(matched),
        "vendor_bank_iban": matched.snapshot.vendor.bank_account_iban
        if matched.snapshot is not None
        else None,
    }
    values.update(changes)
    return TaxonomyRequest.model_validate(values)


def _changed_extraction(field: str, value: object) -> TaxonomyRequest:
    base = _request()
    extraction = base.extraction
    assert extraction is not None
    current = getattr(extraction, field)
    field_change = {
        "value": value,
        "confidence": Decimal(0) if value is None else current.confidence,
    }
    changed = extraction.model_copy(update={field: current.model_copy(update=field_change)})
    matched = matching_request(_current_snapshot()).model_copy(update={"extraction": changed})
    return _request(
        extraction=changed,
        validation=validate_invoice(changed),
        match=match_invoice(matched),
    )


def test_clean_classification_is_reproducible() -> None:
    request = _request()
    first = classify_exceptions(request)
    assert first == classify_exceptions(request)
    assert first.status == "CLEAN" and first.codes == () and first.unresolved == ()
    assert TaxonomyResult.model_validate_json(first.model_dump_json()) == first
    assert request.vendor_bank_iban is not None
    spaced = " ".join(request.vendor_bank_iban.lower())
    assert classify_exceptions(_request(vendor_bank_iban=spaced)).status == "CLEAN"


def test_duplicate_and_missing_po_signals_have_distinct_evidence() -> None:
    base = _request()
    missing = match_invoice(
        matching_request(_current_snapshot()).model_copy(update={"snapshot": None})
    )
    result = classify_exceptions(
        _request(
            match=missing,
            exact_duplicate=True,
            near_duplicate=True,
            stale_po=True,
        )
    )
    assert result.codes == ("DUP_EXACT", "DUP_NEAR", "MISSING_PO", "STALE_PO")
    assert [item.evidence.source for item in result.findings] == [
        "INGEST",
        "SIMILARITY",
        "MATCH_IDENTITY",
        "POLICY",
    ]
    assert result.input_sha256 != classify_exceptions(base).input_sha256
    signal_only = TaxonomyRequest(
        run_id=base.run_id,
        invoice_id=base.invoice_id,
        trace_id=base.trace_id,
        exact_duplicate=True,
    )
    assert classify_exceptions(signal_only).codes == ("DUP_EXACT",)


def test_currency_bank_and_closed_po_are_independently_classified() -> None:
    request = _changed_extraction("currency", "EUR")
    result = classify_exceptions(
        request.model_copy(update={"vendor_bank_iban": "DE00 0000 0000 0000"})
    )
    assert result.codes == ("BANK_CHANGE", "CCY_MM")
    assert result.vendor_bank_sha256 is not None
    assert "DE00" not in result.model_dump_json()
    assert "BANK_CHANGE" in result.model_dump_json()
    closed = _request()
    closed_match = match_invoice(matching_request(snapshot_for("CLOSED")))
    assert classify_exceptions(closed.model_copy(update={"match": closed_match})).codes == (
        "STALE_PO",
    )


def test_price_and_quantity_mismatch_use_line_evidence() -> None:
    base = _request()
    extraction = base.extraction
    assert extraction is not None
    first = extraction.line_items[0]
    assert first.unit_price.value is not None and first.quantity.value is not None
    changed = first.model_copy(
        update={
            "unit_price": first.unit_price.model_copy(
                update={"value": first.unit_price.value + Decimal("100")}
            ),
            "quantity": first.quantity.model_copy(
                update={"value": first.quantity.value + Decimal("100")}
            ),
        }
    )
    extraction = extraction.model_copy(update={"line_items": (changed, *extraction.line_items[1:])})
    matched = matching_request(_current_snapshot()).model_copy(update={"extraction": extraction})
    result = classify_exceptions(
        _request(
            extraction=extraction,
            validation=validate_invoice(extraction),
            match=match_invoice(matched),
        )
    )
    assert "PRICE_MM" in result.codes and "QTY_MM" in result.codes
    assert all(
        item.evidence.line_number == 1
        for item in result.findings
        if item.code in {"PRICE_MM", "QTY_MM"}
    )


def test_tax_and_math_issues_are_classified_without_inventing_missing_values() -> None:
    base = _request()
    extraction = base.extraction
    assert extraction is not None
    tax = extraction.tax_amount.model_copy(update={"value": Decimal("100")})
    total = extraction.total_amount.model_copy(update={"value": Decimal("1")})
    changed = extraction.model_copy(update={"tax_amount": tax, "total_amount": total})
    matched = matching_request(_current_snapshot()).model_copy(update={"extraction": changed})
    result = classify_exceptions(
        _request(
            extraction=changed,
            validation=validate_invoice(changed),
            match=match_invoice(matched),
        )
    )
    assert "TAX_ERR" in result.codes and "MATH_ERR" in result.codes
    assert any(item.evidence.source == "VALIDATION" for item in result.findings)


def test_unknown_and_unmapped_failures_remain_unclassified() -> None:
    request = _changed_extraction("vendor_name", "Other synthetic vendor")
    result = classify_exceptions(request)
    assert result.status == "UNCLASSIFIED"
    assert result.codes == ()
    assert [(ref.source, ref.field) for ref in result.unresolved] == [
        ("MATCH_IDENTITY", "vendor_name")
    ]
    no_evidence = _request(validation=None, match=None, vendor_bank_iban=None)
    assert classify_exceptions(no_evidence).status == "UNCLASSIFIED"
    assert {ref.field for ref in classify_exceptions(no_evidence).unresolved} >= {"not_run"}
    missing_bank = _changed_extraction("bank_account_iban", None)
    assert ("ERP", "bank_account_iban") in [
        (ref.source, ref.field) for ref in classify_exceptions(missing_bank).unresolved
    ]


def test_provenance_mismatch_is_rejected_before_classification() -> None:
    request = _request()
    other = _changed_extraction("vendor_name", "Other synthetic vendor")
    assert other.extraction is not None
    with pytest.raises(ValidationError, match="Validation evidence does not match"):
        TaxonomyRequest.model_validate(
            {**request.model_dump(), "extraction": other.extraction.model_dump()}
        )


def test_codes_follow_stable_taxonomy_order() -> None:
    result = classify_exceptions(_request(exact_duplicate=True, near_duplicate=True, stale_po=True))
    assert result.codes == tuple(code for code in CODE_ORDER if code in result.codes)

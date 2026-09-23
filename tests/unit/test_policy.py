"""Spend, approval, staleness, and evidence precedence are deterministic."""

from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from tests.unit.matching_support import matching_request
from tests.unit.policy_support import policy_request

from invoiceops_agent.schemas.exceptions import TaxonomyRequest
from invoiceops_agent.schemas.policy import PolicyConfig, PolicyRequest, PolicyResult, SpendBand
from invoiceops_agent.tools.exception_taxonomy import classify_exceptions
from invoiceops_agent.tools.matching import match_invoice
from invoiceops_agent.tools.policy import approval_tier, evaluate_policy

pytestmark = pytest.mark.unit


def _amount() -> Decimal:
    value = policy_request().extraction.total_amount.value
    assert value is not None and value > 3
    return value


def test_clean_invoice_is_eligible_and_reproducible() -> None:
    request = policy_request()
    first = evaluate_policy(request)
    assert first == evaluate_policy(request)
    assert first.status == "AUTO_APPROVE_ELIGIBLE"
    assert first.approval_tier == "NONE" and first.findings == ()
    assert PolicyResult.model_validate_json(first.model_dump_json()) == first


def test_approval_bands_use_inclusive_upper_bounds() -> None:
    request = policy_request()
    amount = _amount()
    cases = (
        (("0", "1", "2", "3"), "NONE", "AUTO_APPROVE_ELIGIBLE"),
        (("-0.01", "0", "1", "2"), "AP_MANAGER", "REVIEW"),
        (("-3", "-2", "0", "1"), "PROCUREMENT_DIRECTOR", "REVIEW"),
        (("-3", "-2", "-1", "1"), "DUAL_CONTROL", "REVIEW"),
        (("-4", "-3", "-2", "-1"), "DUAL_CONTROL", "BLOCK"),
    )
    for offsets, tier, status in cases:
        limits = tuple(amount + Decimal(offset) for offset in offsets)
        band = SpendBand(
            currency="GBP",
            auto_approve_limit=limits[0],
            manager_limit=limits[1],
            director_limit=limits[2],
            hard_spend_limit=limits[3],
        )
        config = PolicyConfig(bands=(band,))
        decision = evaluate_policy(request, config)
        assert decision.approval_tier == tier
        assert decision.status == status
    assert "SPEND_CAP_EXCEEDED" in {item.reason for item in decision.findings}


def test_currency_bands_are_explicit_without_exchange_conversion() -> None:
    bands = {band.currency: band for band in PolicyConfig().bands}
    amount = Decimal("5000")
    assert approval_tier(amount, bands["GBP"]) == "NONE"
    assert approval_tier(amount, bands["KWD"]) == "AP_MANAGER"
    assert approval_tier(amount, bands["JPY"]) == "NONE"


def test_exact_duplicate_blocks_even_below_auto_limit() -> None:
    decision = evaluate_policy(policy_request(exact_duplicate=True))
    assert decision.status == "BLOCK"
    assert decision.approval_tier == "NONE"
    assert [item.reason for item in decision.findings] == [
        "EXACT_DUPLICATE",
        "EXCEPTION_PRESENT",
    ]


def test_unknown_and_negative_amounts_never_become_auto_eligible() -> None:
    base = policy_request().extraction
    for value, reason in ((None, "UNKNOWN_AMOUNT"), (Decimal("-1"), "INVALID_AMOUNT")):
        changed_field = base.total_amount.model_copy(
            update={"value": value, "confidence": Decimal(0) if value is None else Decimal(1)}
        )
        changed = base.model_copy(update={"total_amount": changed_field})
        decision = evaluate_policy(policy_request(extraction=changed))
        assert decision.status == "REVIEW"
        assert decision.approval_tier == "UNKNOWN"
        assert reason in {item.reason for item in decision.findings}


def test_missing_po_requires_review_without_guessing_its_age() -> None:
    request = policy_request()
    source = matching_request().model_copy(
        update={"extraction": request.extraction, "snapshot": None}
    )
    match = match_invoice(source)
    taxonomy = classify_exceptions(
        TaxonomyRequest(
            run_id=request.run_id,
            invoice_id=request.invoice_id,
            trace_id=request.trace_id,
            extraction=request.extraction,
            validation=request.validation,
            match=match,
            vendor_bank_iban=request.snapshot.vendor.bank_account_iban
            if request.snapshot is not None
            else None,
        )
    )
    changed = PolicyRequest.model_validate(
        {**request.model_dump(), "snapshot": None, "match": match, "taxonomy": taxonomy}
    )
    decision = evaluate_policy(changed)
    assert decision.status == "REVIEW"
    assert "MATCH_NOT_PASSED" in {item.reason for item in decision.findings}
    assert "STALE_PO" not in {item.reason for item in decision.findings}


def test_closed_cancelled_stale_and_future_po_routing() -> None:
    request = policy_request()
    assert request.snapshot is not None
    order = request.snapshot.purchase_order
    for status, expected in (("CLOSED", "CLOSED_PO"), ("CANCELLED", "CANCELLED_PO")):
        snapshot = request.snapshot.model_copy(
            update={"purchase_order": order.model_copy(update={"status": status})}
        )
        # Rebuild the matcher/taxonomy fingerprints for the changed ERP status.
        match = match_invoice(matching_request(snapshot))
        taxonomy = classify_exceptions(
            TaxonomyRequest(
                run_id=request.run_id,
                invoice_id=request.invoice_id,
                trace_id=request.trace_id,
                extraction=request.extraction,
                validation=request.validation,
                match=match,
                vendor_bank_iban=snapshot.vendor.bank_account_iban,
            )
        )
        changed = PolicyRequest.model_validate(
            {**request.model_dump(), "snapshot": snapshot, "match": match, "taxonomy": taxonomy}
        )
        result = evaluate_policy(changed)
        assert expected in {item.reason for item in result.findings}
        assert result.status == ("BLOCK" if status == "CANCELLED" else "REVIEW")
    stale = evaluate_policy(
        request.model_copy(update={"as_of": order.issued_on + timedelta(days=366)})
    )
    assert stale.status == "REVIEW"
    assert "STALE_PO" in {item.reason for item in stale.findings}
    boundary = evaluate_policy(
        request.model_copy(update={"as_of": order.issued_on + timedelta(days=365)})
    )
    assert boundary.status == "AUTO_APPROVE_ELIGIBLE"
    future = evaluate_policy(
        request.model_copy(update={"as_of": order.issued_on - timedelta(days=1)})
    )
    assert "FUTURE_PO" in {item.reason for item in future.findings}


def test_provenance_and_config_reject_inconsistent_inputs() -> None:
    request = policy_request()
    with pytest.raises(ValidationError, match="ERP snapshot presence does not match"):
        PolicyRequest.model_validate({**request.model_dump(), "snapshot": None})
    with pytest.raises(ValidationError, match="ascend strictly"):
        SpendBand(currency="GBP", manager_limit=Decimal("1000"), director_limit=Decimal("900"))
    with pytest.raises(ValidationError):
        SpendBand.model_validate({"currency": "GBP", "auto_approve_limit": 100.0})
    with pytest.raises(ValidationError, match="unique currencies"):
        PolicyConfig(bands=(SpendBand(currency="GBP"), SpendBand(currency="GBP")))
    unknown_currency = evaluate_policy(request, PolicyConfig(bands=(SpendBand(currency="USD"),)))
    assert unknown_currency.status == "REVIEW"
    assert unknown_currency.approval_tier == "UNKNOWN"
    assert "UNSUPPORTED_CURRENCY" in {item.reason for item in unknown_currency.findings}

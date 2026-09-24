"""Pure confidence gating with explicit Decimal normalization and abstention."""

from decimal import Decimal, localcontext

from invoiceops_agent.schemas.common import model_digest
from invoiceops_agent.schemas.extraction import InvoiceExtraction
from invoiceops_agent.schemas.gate import (
    CompositeGateConfig,
    CompositeGateResult,
    GateConfig,
    GateResult,
)
from invoiceops_agent.schemas.matching import MatchResult
from invoiceops_agent.schemas.policy import PolicyRequest, PolicyResult


def minimum_field_confidence(extraction: InvoiceExtraction) -> Decimal:
    """Score observed fields; validation and policy handle missing required values."""
    confidences = [
        field.confidence
        for field in (
            extraction.vendor_name,
            extraction.vendor_tax_id,
            extraction.bank_account_iban,
            extraction.invoice_number,
            extraction.po_number,
            extraction.currency,
            extraction.invoice_date,
            extraction.due_date,
            extraction.subtotal,
            extraction.tax_amount,
            extraction.total_amount,
        )
        if field.value is not None
    ]
    for line in extraction.line_items:
        confidences.extend(
            field.confidence
            for field in (
                line.description,
                line.quantity,
                line.unit_price,
                line.tax_rate,
                line.line_total,
            )
            if field.value is not None
        )
    return min(confidences) if confidences else Decimal(0)


def normalized_match_delta(match: MatchResult, denominator_floor: Decimal) -> Decimal:
    """Largest equality gap; directional upper bounds are enforced by matching policy."""
    equal_checks = tuple(check for check in match.numeric_checks if check.rule == "equal")
    if not match.snapshot_found or not equal_checks:
        return Decimal(1)
    largest = Decimal(0)
    with localcontext() as context:
        context.prec = 28
        for check in equal_checks:
            if (
                check.status == "UNKNOWN"
                or check.expected is None
                or check.actual is None
                or check.difference is None
            ):
                return Decimal(1)
            denominator = max(abs(check.expected), abs(check.actual), denominator_floor)
            ratio = min(Decimal(1), abs(check.difference) / denominator)
            largest = max(largest, ratio)
        return largest


def evaluate_composite_gate(
    request: PolicyRequest,
    policy: PolicyResult,
    config: CompositeGateConfig | None = None,
) -> CompositeGateResult:
    """Score three pinned signals; never override an ineligible policy decision."""
    rules = config if config is not None else CompositeGateConfig()
    if policy.input_sha256 != model_digest(request):
        raise ValueError("Policy evidence does not belong to the gate inputs")
    extraction = request.extraction
    match = request.match
    extraction_sha256 = model_digest(extraction)
    field_term = minimum_field_confidence(extraction)
    match_delta = normalized_match_delta(match, rules.delta_denominator_floor)
    policy_term = {
        "AUTO_APPROVE_ELIGIBLE": Decimal(1),
        "REVIEW": Decimal("0.5"),
        "BLOCK": Decimal(0),
    }[policy.status]
    score = rules.score(field_term, match_delta, policy_term)
    reason = (
        "POLICY"
        if policy.status != "AUTO_APPROVE_ELIGIBLE"
        else "AUTO_DISABLED"
        if not rules.auto_approval_enabled
        else "BELOW_THRESHOLD"
        if score < rules.threshold
        else "ELIGIBLE"
    )
    return CompositeGateResult(
        route="AUTO_APPROVE" if reason == "ELIGIBLE" else "REVIEW",
        reason=reason,
        score=score,
        minimum_field_confidence=field_term,
        normalized_match_delta=match_delta,
        policy_severity_term=policy_term,
        policy_status=policy.status,
        extraction_sha256=extraction_sha256,
        match_sha256=model_digest(match),
        policy_sha256=model_digest(policy),
        config_sha256=model_digest(rules),
        config=rules,
    )


def evaluate_provisional_gate(
    extraction: InvoiceExtraction, policy: PolicyResult, config: GateConfig | None = None
) -> GateResult:
    rules = config if config is not None else GateConfig()
    confidences = [
        field.confidence
        for field in (
            extraction.vendor_name,
            extraction.vendor_tax_id,
            extraction.bank_account_iban,
            extraction.invoice_number,
            extraction.po_number,
            extraction.currency,
            extraction.invoice_date,
            extraction.due_date,
            extraction.subtotal,
            extraction.tax_amount,
            extraction.total_amount,
        )
        if field.value is not None
    ]
    for line in extraction.line_items:
        confidences.extend(
            (
                line.description.confidence,
                line.quantity.confidence,
                line.unit_price.confidence,
                line.tax_rate.confidence,
                line.line_total.confidence,
            )
        )
    minimum = min(confidences) if confidences else None
    reason = (
        "POLICY"
        if policy.status != "AUTO_APPROVE_ELIGIBLE"
        else "AUTO_DISABLED"
        if not rules.auto_approval_enabled
        else "LOW_CONFIDENCE"
        if minimum is None or minimum < rules.minimum_field_confidence
        else "ELIGIBLE"
    )
    return GateResult(
        route="AUTO_APPROVE" if reason == "ELIGIBLE" else "REVIEW",
        minimum_observed_confidence=minimum,
        reason=reason,
        extraction_sha256=model_digest(extraction),
        policy_sha256=model_digest(policy),
        config_sha256=model_digest(rules),
        config=rules,
    )

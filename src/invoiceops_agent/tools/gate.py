"""Provisional fail-closed routing while composite confidence is Phase 2.7 work."""

from invoiceops_agent.schemas.common import model_digest
from invoiceops_agent.schemas.extraction import InvoiceExtraction
from invoiceops_agent.schemas.gate import GateConfig, GateResult
from invoiceops_agent.schemas.policy import PolicyResult


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

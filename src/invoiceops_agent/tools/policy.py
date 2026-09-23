"""Pure, versioned invoice approval and purchase-order controls."""

from decimal import Decimal

from invoiceops_agent.schemas.common import model_digest
from invoiceops_agent.schemas.policy import (
    ApprovalTier,
    PolicyConfig,
    PolicyFinding,
    PolicyRequest,
    PolicyResult,
    PolicyStatus,
    SpendBand,
)


def approval_tier(amount: Decimal | None, band: SpendBand | None) -> ApprovalTier:
    """Use inclusive upper bounds and never approve an unknown amount."""
    if amount is None or amount < 0 or band is None:
        return "UNKNOWN"
    if amount <= band.auto_approve_limit:
        return "NONE"
    if amount <= band.manager_limit:
        return "AP_MANAGER"
    if amount <= band.director_limit:
        return "PROCUREMENT_DIRECTOR"
    return "DUAL_CONTROL"


def evaluate_policy(request: PolicyRequest, config: PolicyConfig | None = None) -> PolicyResult:
    """Evaluate supplied evidence without I/O, model inference, or a clock read."""
    rules = config if config is not None else PolicyConfig()
    findings: list[PolicyFinding] = []
    codes = set(request.taxonomy.codes)
    if "DUP_EXACT" in codes:
        findings.append(PolicyFinding(reason="EXACT_DUPLICATE", field="content_hash"))

    snapshot = request.snapshot
    if snapshot is not None:
        order = snapshot.purchase_order
        if order.status == "CANCELLED":
            findings.append(PolicyFinding(reason="CANCELLED_PO", field="po_status"))
        elif order.status == "CLOSED":
            findings.append(PolicyFinding(reason="CLOSED_PO", field="po_status"))
        age_days = (request.as_of - order.issued_on).days
        if age_days < 0:
            findings.append(PolicyFinding(reason="FUTURE_PO", field="issued_on"))
        elif age_days > rules.max_po_age_days:
            findings.append(PolicyFinding(reason="STALE_PO", field="issued_on"))

    amount = request.extraction.total_amount.value
    currency = request.extraction.currency.value
    band = next((item for item in rules.bands if item.currency == currency), None)
    tier = approval_tier(amount, band)
    if band is None:
        findings.append(PolicyFinding(reason="UNSUPPORTED_CURRENCY", field="currency"))
    if amount is None:
        findings.append(PolicyFinding(reason="UNKNOWN_AMOUNT", field="total_amount"))
    elif amount < 0:
        findings.append(PolicyFinding(reason="INVALID_AMOUNT", field="total_amount"))
    else:
        if band is not None and amount > band.hard_spend_limit:
            findings.append(PolicyFinding(reason="SPEND_CAP_EXCEEDED", field="total_amount"))
        if tier not in {"NONE", "UNKNOWN"}:
            findings.append(PolicyFinding(reason="APPROVAL_REQUIRED", field="total_amount"))

    if request.taxonomy.status == "EXCEPTION":
        findings.append(PolicyFinding(reason="EXCEPTION_PRESENT", field="taxonomy"))
    if request.taxonomy.unresolved:
        findings.append(PolicyFinding(reason="UNRESOLVED_EVIDENCE", field="taxonomy"))
    if request.validation.status != "PASS":
        findings.append(PolicyFinding(reason="VALIDATION_FAILED", field="validation"))
    if request.match.status != "PASS":
        findings.append(PolicyFinding(reason="MATCH_NOT_PASSED", field="match"))

    blocking = {"EXACT_DUPLICATE", "CANCELLED_PO", "SPEND_CAP_EXCEEDED"}
    status: PolicyStatus = (
        "BLOCK"
        if any(item.reason in blocking for item in findings)
        else "REVIEW"
        if findings
        else "AUTO_APPROVE_ELIGIBLE"
    )
    return PolicyResult(
        status=status,
        approval_tier=tier,
        currency=currency,
        amount=amount,
        findings=tuple(findings),
        as_of=request.as_of,
        input_sha256=model_digest(request),
        config_sha256=model_digest(rules),
        config=rules,
    )

"""Pure queue priority and SLA derivation from audited exception evidence."""

from datetime import datetime, timedelta

from pydantic import JsonValue

from invoiceops_agent.schemas.common import model_digest
from invoiceops_agent.schemas.exception_queue import QueueConfig, QueueProjection
from invoiceops_agent.schemas.exceptions import TaxonomyResult
from invoiceops_agent.schemas.gate import GateOutcome
from invoiceops_agent.schemas.policy import PolicyResult

_CRITICAL_CODES = frozenset({"BANK_CHANGE", "DUP_NEAR"})


def project_exception(
    *,
    now: datetime,
    taxonomy: TaxonomyResult | None,
    policy: PolicyResult | None,
    gate: GateOutcome | None,
    extraction_escalated: bool,
    recommendation: dict[str, JsonValue],
    config: QueueConfig | None = None,
) -> QueueProjection:
    if now.utcoffset() is None:
        raise ValueError("Exception queue evaluation time must be timezone aware")
    rules = config if config is not None else QueueConfig()
    codes = taxonomy.codes if taxonomy is not None else ()
    if (policy is not None and policy.status == "BLOCK") or _CRITICAL_CODES.intersection(codes):
        priority = 3
    elif codes or extraction_escalated or (policy is not None and policy.status == "REVIEW"):
        priority = 2
    else:
        priority = 1
    hours = (
        rules.critical_sla_hours
        if priority == 3
        else rules.high_sla_hours
        if priority == 2
        else rules.normal_sla_hours
    )
    exception_type = (
        codes[0] if codes else "EXTRACTION_ESCALATION" if extraction_escalated else "LOW_CONFIDENCE"
    )
    return QueueProjection(
        exception_type=exception_type,
        priority=priority,
        sla_due_at=now + timedelta(hours=hours),
        evidence={
            "taxonomy_sha256": model_digest(taxonomy) if taxonomy is not None else None,
            "policy_sha256": model_digest(policy) if policy is not None else None,
            "gate_sha256": model_digest(gate) if gate is not None else None,
            "extraction_escalated": extraction_escalated,
        },
        recommendation=recommendation,
        config=rules,
    )

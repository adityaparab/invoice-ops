"""Gather bounded, deterministic review facts without leaking raw invoice text."""

from collections import Counter

from invoiceops_agent.schemas.exceptions import TaxonomyResult
from invoiceops_agent.schemas.matching import MatchResult
from invoiceops_agent.schemas.policy import PolicyResult
from invoiceops_agent.schemas.triage import TriageEvidence, TriageFact
from invoiceops_agent.schemas.validation import ValidationResult


def gather_triage_evidence(
    *,
    taxonomy: TaxonomyResult | None,
    policy: PolicyResult | None,
    match: MatchResult | None,
    validation: ValidationResult | None,
    extraction_escalated: bool,
) -> TriageEvidence:
    facts: list[TriageFact] = [
        TriageFact(ref="workflow:review", detail="Human review is required"),
        TriageFact(
            ref="extraction:status",
            detail="ESCALATED" if extraction_escalated else "COMPLETED",
        ),
    ]
    if taxonomy is not None:
        facts.append(TriageFact(ref="taxonomy:status", detail=taxonomy.status))
        code_counts: Counter[str] = Counter()
        for tax_finding in taxonomy.findings:
            code_counts[tax_finding.code] += 1
            suffix = (
                f" line {tax_finding.evidence.line_number}"
                if tax_finding.evidence.line_number
                else ""
            )
            facts.append(
                TriageFact(
                    ref=(
                        f"taxonomy:{tax_finding.code}"
                        if code_counts[tax_finding.code] == 1
                        else f"taxonomy:{tax_finding.code}:{code_counts[tax_finding.code]}"
                    ),
                    detail=f"{tax_finding.evidence.source}.{tax_finding.evidence.field}{suffix}",
                )
            )
    if policy is not None:
        facts.append(TriageFact(ref="policy:status", detail=policy.status))
        for policy_finding in policy.findings:
            facts.append(
                TriageFact(ref=f"policy:{policy_finding.reason}", detail=policy_finding.field)
            )
    if match is not None:
        facts.append(TriageFact(ref="match:status", detail=match.status))
        for index, identity_check in enumerate(match.identity_checks):
            if identity_check.status != "MATCH":
                facts.append(
                    TriageFact(
                        ref=f"match:identity:{index}",
                        detail=f"{identity_check.field} {identity_check.status}",
                    )
                )
        for index, numeric_check in enumerate(match.numeric_checks):
            if numeric_check.status != "MATCH":
                facts.append(
                    TriageFact(
                        ref=f"match:numeric:{index}",
                        detail=f"{numeric_check.field} {numeric_check.status}; "
                        f"delta={numeric_check.difference}; "
                        f"tolerance={numeric_check.tolerance}",
                    )
                )
    if validation is not None:
        facts.append(TriageFact(ref="validation:status", detail=validation.status))
        for index, issue in enumerate(validation.issues):
            facts.append(
                TriageFact(
                    ref=f"validation:issue:{index}",
                    detail=f"{issue.code} {issue.field}",
                )
            )
    return TriageEvidence(facts=tuple(facts[:150]))

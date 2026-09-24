"""Golden diagnostics keep labels, score bins, and judge provenance distinct."""

import asyncio
from decimal import Decimal

import pytest
from eval.diagnostics import score_diagnostics
from eval.judge_triage import TriageJudgeReport, judge_pipeline, triage_requests
from eval.metrics import MetricEvidenceError
from eval.runners.schema import RunRecord
from tests.unit.test_eval_judge import FakeJudgeGateway
from tests.unit.test_primary_metrics import MANIFEST, MANIFEST_SHA, START, _record, _report, _sample

from invoiceops_agent.schemas.common import model_digest
from invoiceops_agent.schemas.eval_judge import TriageJudgeResult, TriageJudgeRubric
from invoiceops_agent.schemas.triage import TriageDraft, TriageEvidence, TriageFact

pytestmark = pytest.mark.unit


def _with_gate(record: RunRecord, score: str, policy_status: str) -> RunRecord:
    detail = record.detail.model_copy(
        update={
            "evidence": {
                **record.detail.evidence,
                "gate.completed": {"score": score, "policy_status": policy_status},
            }
        }
    )
    return record.model_copy(update={"detail": detail})


def test_code_confusion_fields_calibration_and_tau_sweep() -> None:
    clean = _sample()
    anomaly = _sample(code="PRICE_MM")
    duplicate = _sample(code="DUP_EXACT")
    clean_record = _with_gate(_record(clean, route="ARCHIVE"), "0.90", "AUTO_APPROVE_ELIGIBLE")
    anomaly_record = _with_gate(
        _record(anomaly, route="REVIEW", code="PRICE_MM"),
        "0.80",
        "AUTO_APPROVE_ELIGIBLE",
    )
    report = _report(
        (clean_record, anomaly_record, _record(duplicate, route="REJECT")),
        offset_minutes=0,
    )
    scored = score_diagnostics(MANIFEST, MANIFEST_SHA, report)
    assert len(scored.anomaly_confusion) == 10
    assert next(row for row in scored.anomaly_confusion if row.code == "PRICE_MM").tp == 1
    assert next(row for row in scored.anomaly_confusion if row.code == "DUP_EXACT").tp == 1
    assert len(scored.field_by_tier) == 48
    assert sum(row.sample_count for row in scored.calibration) == 2
    assert len(scored.tau_sweep) == 101
    assert scored.tau_sweep[0].routed_exception_recall == Decimal("0.5")
    assert scored.tau_sweep[85].stp_rate == 1
    assert scored.tau_sweep[95].false_escalation_rate == 1
    assert scored.judge is None


def test_judge_report_requires_exact_audited_triage_and_source_hash() -> None:
    sample = _sample(code="PRICE_MM")
    record = _record(sample, route="REVIEW", code="PRICE_MM")
    evidence = TriageEvidence(
        facts=(TriageFact(ref="taxonomy:PRICE_MM", detail="Unit price differs"),)
    )
    draft = TriageDraft(
        recommended_action="ESCALATE",
        summary="Price variance requires review",
        rationale="Invoice and PO prices differ",
        evidence_refs=("taxonomy:PRICE_MM",),
    )
    detail = record.detail.model_copy(
        update={
            "evidence": {
                **record.detail.evidence,
                "triage.prepared": {
                    "codes": ["PRICE_MM"],
                    "triage": {
                        "status": "DRAFT",
                        "draft": draft.model_dump(mode="json"),
                        "cost_usd": "0.003",
                    },
                    "evidence": evidence.model_dump(mode="json"),
                },
            }
        }
    )
    pipeline = _report((record.model_copy(update={"detail": detail}),), offset_minutes=0)
    judge_request = triage_requests(pipeline)[0]
    replayed = asyncio.run(judge_pipeline(pipeline, "b" * 64, FakeJudgeGateway()))
    assert replayed.eligible_count == 1 and replayed.results[0].status == "SCORED"
    result = TriageJudgeResult(
        sample_id=sample.sample_id,
        status="SCORED",
        rubric=TriageJudgeRubric(
            evidence_support=2,
            action_safety=2,
            clarity=1,
            rationale="Facts support human review.",
        ),
        error_type=None,
        evidence_sha256=model_digest(judge_request),
        model_version="synthetic-judge@v1",
    )
    judge = TriageJudgeReport(
        manifest_sha256=MANIFEST_SHA,
        model_class="local-dev",
        source_report_sha256="b" * 64,
        scored_at=START,
        eligible_count=1,
        results=(result,),
    )
    scored = score_diagnostics(
        MANIFEST,
        MANIFEST_SHA,
        pipeline,
        pipeline_sha256="b" * 64,
        judge_report=judge,
        judge_report_sha256="c" * 64,
    )
    assert scored.judge is not None
    assert scored.judge.mean_total == 5
    assert scored.judge.scored_count == 1
    with pytest.raises(MetricEvidenceError, match="does not belong"):
        score_diagnostics(
            MANIFEST,
            MANIFEST_SHA,
            pipeline,
            pipeline_sha256="0" * 64,
            judge_report=judge,
            judge_report_sha256="c" * 64,
        )

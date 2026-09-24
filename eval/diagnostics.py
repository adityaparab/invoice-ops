"""Per-code, per-field, calibration, and threshold diagnostics for golden runs."""

import argparse
import hashlib
import logging
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eval.golden.schema import AnomalyCode, GoldenManifest, GoldenSample
from eval.judge_triage import TriageJudgeReport, triage_requests
from eval.metrics import FieldCounts, MetricEvidenceError, _codes, _extraction, score_fields
from eval.metrics import score_primary_metrics as validate_primary_report
from eval.runners.schema import ModelClass, PipelineReport, RunRecord
from invoiceops_agent.artifacts import write_new_artifact
from invoiceops_agent.schemas.common import model_digest
from invoiceops_agent.schemas.eval_judge import TriageJudgeResult

logger = logging.getLogger(__name__)
ANOMALY_CODES: tuple[AnomalyCode, ...] = (
    "DUP_EXACT",
    "DUP_NEAR",
    "PRICE_MM",
    "QTY_MM",
    "MISSING_PO",
    "BANK_CHANGE",
    "CCY_MM",
    "TAX_ERR",
    "MATH_ERR",
    "STALE_PO",
)
TIERS: tuple[Literal["A", "B", "C"], ...] = ("A", "B", "C")


class DiagnosticModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class AnomalyConfusion(DiagnosticModel):
    code: AnomalyCode
    tp: int = Field(ge=0)
    fp: int = Field(ge=0)
    fn: int = Field(ge=0)
    tn: int = Field(ge=0)


class FieldDiagnostic(DiagnosticModel):
    field: str
    tier: Literal["A", "B", "C"]
    sample_count: int = Field(ge=0)
    tp: int = Field(ge=0)
    fp: int = Field(ge=0)
    fn: int = Field(ge=0)
    f1: Decimal | None


class CalibrationBin(DiagnosticModel):
    lower: Decimal = Field(ge=0, le=1)
    upper: Decimal = Field(ge=0, le=1)
    sample_count: int = Field(ge=0)
    mean_score: Decimal | None
    observed_clean_rate: Decimal | None


class TauSweepPoint(DiagnosticModel):
    threshold: Decimal = Field(ge=0, le=1)
    routed_exception_recall: Decimal | None
    false_escalation_rate: Decimal | None
    stp_rate: Decimal | None


class JudgeSummary(DiagnosticModel):
    eligible_count: int = Field(ge=0, le=500)
    scored_count: int = Field(ge=0, le=500)
    unavailable_count: int = Field(ge=0, le=500)
    mean_total: Decimal | None
    results: tuple[TriageJudgeResult, ...]


class DiagnosticReport(DiagnosticModel):
    version: Literal["golden-diagnostics@v1", "golden-diagnostics@v2"] = "golden-diagnostics@v2"
    dataset_version: Literal["golden/v1.0.0", "golden/v1.0.1"] = "golden/v1.0.1"
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: Literal["live", "recorded"]
    model_class: ModelClass | None = None
    sample_count: int = Field(ge=1, le=500)
    anomaly_confusion: tuple[AnomalyConfusion, ...]
    field_by_tier: tuple[FieldDiagnostic, ...]
    calibration: tuple[CalibrationBin, ...]
    tau_sweep: tuple[TauSweepPoint, ...]
    judge: JudgeSummary | None = None
    judge_report_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    caveats: tuple[str, ...] = ()


def _detected_codes(record: RunRecord) -> set[str]:
    codes = _codes(record)
    if record.upload.duplicate:
        codes.add("DUP_EXACT")
    return codes


def confusion(
    samples: tuple[GoldenSample, ...], records: dict[str, RunRecord]
) -> tuple[AnomalyConfusion, ...]:
    routing = [sample for sample in samples if sample.routing_eligible]
    output = []
    for code in ANOMALY_CODES:
        tp = fp = fn = tn = 0
        for sample in routing:
            expected = code in sample.anomaly_codes
            observed = code in _detected_codes(records[sample.sample_id])
            if expected and observed:
                tp += 1
            elif expected:
                fn += 1
            elif observed:
                fp += 1
            else:
                tn += 1
        output.append(AnomalyConfusion(code=code, tp=tp, fp=fp, fn=fn, tn=tn))
    return tuple(output)


def field_by_tier(
    samples: tuple[GoldenSample, ...], records: dict[str, RunRecord]
) -> tuple[FieldDiagnostic, ...]:
    totals: dict[tuple[str, str], FieldCounts] = defaultdict(FieldCounts)
    tier_counts = {tier: 0 for tier in TIERS}
    field_names = tuple(score_fields(samples[0].label, None))
    for sample in samples:
        record = records[sample.sample_id]
        if record.upload.duplicate:
            continue
        tier_counts[sample.quality_tier] += 1
        for field, counts in score_fields(sample.label, _extraction(record)).items():
            totals[(sample.quality_tier, field)] += counts
    return tuple(
        FieldDiagnostic(
            field=field,
            tier=tier,
            sample_count=tier_counts[tier],
            tp=totals[(tier, field)].tp,
            fp=totals[(tier, field)].fp,
            fn=totals[(tier, field)].fn,
            f1=totals[(tier, field)].f1,
        )
        for tier in TIERS
        for field in field_names
    )


def _gate_score(record: RunRecord) -> tuple[Decimal, bool] | None:
    payload = record.detail.evidence.get("gate.completed")
    if payload is None:
        return None
    raw = payload.get("score")
    if raw is None:
        raise MetricEvidenceError("Committed gate evidence lacks its score")
    try:
        score = Decimal(str(raw))
    except InvalidOperation:
        raise MetricEvidenceError("Committed gate score is invalid") from None
    if not score.is_finite() or not 0 <= score <= 1:
        raise MetricEvidenceError("Committed gate score is outside zero to one")
    status = payload.get("policy_status")
    if status not in {"AUTO_APPROVE_ELIGIBLE", "REVIEW", "BLOCK"}:
        raise MetricEvidenceError("Committed gate policy status is invalid")
    return score, status == "AUTO_APPROVE_ELIGIBLE"


def calibration(
    samples: tuple[GoldenSample, ...], records: dict[str, RunRecord]
) -> tuple[CalibrationBin, ...]:
    groups: list[list[tuple[Decimal, bool]]] = [[] for _ in range(10)]
    for sample in samples:
        if not sample.routing_eligible:
            continue
        score = _gate_score(records[sample.sample_id])
        if score is None:
            continue
        index = min(int(score[0] * 10), 9)
        groups[index].append((score[0], not sample.anomaly_codes))
    output = []
    for index, group in enumerate(groups):
        count = len(group)
        output.append(
            CalibrationBin(
                lower=Decimal(index) / 10,
                upper=Decimal(index + 1) / 10,
                sample_count=count,
                mean_score=sum((score for score, _ in group), Decimal(0)) / count
                if count
                else None,
                observed_clean_rate=Decimal(sum(clean for _, clean in group)) / count
                if count
                else None,
            )
        )
    return tuple(output)


def tau_sweep(
    samples: tuple[GoldenSample, ...], records: dict[str, RunRecord]
) -> tuple[TauSweepPoint, ...]:
    routing = [sample for sample in samples if sample.routing_eligible]
    clean = [sample for sample in routing if not sample.anomaly_codes]
    anomalous = [sample for sample in routing if sample.anomaly_codes]
    gates = {sample.sample_id: _gate_score(records[sample.sample_id]) for sample in routing}

    def would_auto(sample: GoldenSample, threshold: Decimal) -> bool:
        gate = gates[sample.sample_id]
        return gate is not None and gate[1] and gate[0] >= threshold

    output = []
    for number in range(101):
        threshold = Decimal(number) / 100
        false_escalation = sum(not would_auto(sample, threshold) for sample in clean)
        safe_routing = sum(not would_auto(sample, threshold) for sample in anomalous)
        output.append(
            TauSweepPoint(
                threshold=threshold,
                routed_exception_recall=(
                    Decimal(safe_routing) / len(anomalous) if anomalous else None
                ),
                false_escalation_rate=(Decimal(false_escalation) / len(clean) if clean else None),
                stp_rate=(Decimal(len(clean) - false_escalation) / len(clean) if clean else None),
            )
        )
    return tuple(output)


def _judge_summary(
    pipeline: PipelineReport,
    manifest_sha256: str,
    pipeline_sha256: str | None,
    judge_report: TriageJudgeReport | None,
) -> JudgeSummary | None:
    if judge_report is None:
        return None
    if (
        judge_report.manifest_sha256 != manifest_sha256
        or judge_report.model_class != pipeline.model_class
        or judge_report.source_report_sha256 != pipeline_sha256
    ):
        raise MetricEvidenceError("Judge report does not belong to this pipeline report")
    requests = {request.sample_id: request for request in triage_requests(pipeline)}
    if judge_report.eligible_count != len(requests):
        raise MetricEvidenceError("Judge eligibility count differs from audited triage")
    seen: set[str] = set()
    for result in judge_report.results:
        request = requests.get(result.sample_id)
        if (
            request is None
            or result.sample_id in seen
            or result.evidence_sha256 != model_digest(request)
        ):
            raise MetricEvidenceError("Judge result differs from audited triage evidence")
        seen.add(result.sample_id)
    scored = [result for result in judge_report.results if result.rubric is not None]
    total = sum(result.rubric.total for result in scored if result.rubric is not None)
    return JudgeSummary(
        eligible_count=judge_report.eligible_count,
        scored_count=len(scored),
        unavailable_count=len(judge_report.results) - len(scored),
        mean_total=Decimal(total) / len(scored) if scored else None,
        results=judge_report.results,
    )


def score_diagnostics(
    manifest: GoldenManifest,
    manifest_sha256: str,
    pipeline: PipelineReport,
    *,
    pipeline_sha256: str | None = None,
    judge_report: TriageJudgeReport | None = None,
    judge_report_sha256: str | None = None,
) -> DiagnosticReport:
    validate_primary_report(manifest, manifest_sha256, (pipeline,))
    if (judge_report is None) != (judge_report_sha256 is None):
        raise MetricEvidenceError("Judge report and checksum must be supplied together")
    records = {record.sample_id: record for record in pipeline.samples}
    samples = tuple(sample for sample in manifest.samples if sample.sample_id in records)
    calibrated = calibration(samples, records)
    judge = _judge_summary(pipeline, manifest_sha256, pipeline_sha256, judge_report)
    caveats = [
        "Composite score is a ranking signal, not a calibrated probability.",
        "Threshold sweep measures routing safety; primary recall requires injected codes.",
    ]
    if pipeline.mode == "recorded":
        caveats.append("Recorded cassettes are smoke evidence, not live model quality.")
    if len(samples) < 500:
        caveats.append("Partial selections are diagnostic only.")
    if judge is None:
        caveats.append("The versioned triage judge was not run for this report.")
    elif judge.scored_count < judge.eligible_count:
        caveats.append("Triage judge coverage is incomplete.")
    return DiagnosticReport(
        version="golden-diagnostics@v2" if pipeline.model_class else "golden-diagnostics@v1",
        dataset_version=manifest.version,
        manifest_sha256=manifest_sha256,
        mode=pipeline.mode,
        model_class=pipeline.model_class,
        sample_count=len(samples),
        anomaly_confusion=confusion(samples, records),
        field_by_tier=field_by_tier(samples, records),
        calibration=calibrated,
        tau_sweep=tau_sweep(samples, records),
        judge=judge,
        judge_report_sha256=judge_report_sha256,
        caveats=tuple(caveats),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("eval/golden/v1.0.1/manifest.json"))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--judge-report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        raw = args.manifest.read_bytes()
        manifest = GoldenManifest.model_validate_json(raw)
        pipeline_raw = args.input.read_bytes()
        pipeline = PipelineReport.model_validate_json(pipeline_raw)
        judge_raw = args.judge_report.read_bytes() if args.judge_report else None
        judge_report = TriageJudgeReport.model_validate_json(judge_raw) if judge_raw else None
        judge_sha = hashlib.sha256(judge_raw).hexdigest() if judge_raw else None
        report = score_diagnostics(
            manifest,
            hashlib.sha256(raw).hexdigest(),
            pipeline,
            pipeline_sha256=hashlib.sha256(pipeline_raw).hexdigest(),
            judge_report=judge_report,
            judge_report_sha256=judge_sha,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_new_artifact(args.output, (report.model_dump_json(indent=2) + "\n").encode())
    except (MetricEvidenceError, ValidationError, OSError, ValueError) as error:
        logger.error("golden_diagnostics_failed error_type=%s", type(error).__name__)
        return 1
    logger.info("golden_diagnostics_written output=%s samples=%d", args.output, report.sample_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

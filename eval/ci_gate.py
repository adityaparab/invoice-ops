"""Compare complete live primary reports against the main-branch baseline."""

import argparse
import hashlib
import logging
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eval.metrics import PRIMARY_TARGETS, MetricKey, PrimaryMetric, PrimaryMetricsReport
from eval.runners.schema import ModelClass
from invoiceops_agent.artifacts import write_new_artifact

logger = logging.getLogger(__name__)

# Rates use percentage points. Cost and latency use 0.5% of their versioned target
# because their raw units are dollars and seconds, respectively.
RATE_TOLERANCE = Decimal("0.005")
RELATIVE_TOLERANCE = Decimal("0.005")


class GateEvidenceError(ValueError):
    """Reports cannot support a release regression decision."""


class GateRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    key: MetricKey
    baseline: Decimal
    candidate: Decimal
    delta: Decimal
    tolerance: Decimal
    target: Decimal
    direction: Literal["min", "max"]
    floor_breached: bool
    regression_breached: bool


class GateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    version: Literal["ci-gate@v1"] = "ci-gate@v1"
    dataset_version: Literal["golden/v1.0.0", "golden/v1.0.1"] = "golden/v1.0.1"
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_class: ModelClass
    baseline_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    passed: bool
    rows: tuple[GateRow, ...]


def _rows(report: PrimaryMetricsReport) -> dict[MetricKey, PrimaryMetric]:
    if (
        report.version != "primary-metrics@v2"
        or report.mode != "live"
        or report.model_class is None
        or not report.complete_suite
        or report.report_count != 3
        or report.sample_count != 500
    ):
        raise GateEvidenceError(
            "Gate requires a tagged, complete 500-invoice, three-run live report"
        )
    rows = {row.key: row for row in report.metrics}
    if len(rows) != len(report.metrics) or set(rows) != set(PRIMARY_TARGETS):
        raise GateEvidenceError("Report has missing or duplicate primary metrics")
    for key, row in rows.items():
        target = PRIMARY_TARGETS[key]
        if row.direction != target.direction or row.target != target.value:
            raise GateEvidenceError(f"Versioned target differs for {key}")
        if row.value is None:
            raise GateEvidenceError(f"Primary metric unavailable: {key}")
        if row.evidence_count == 0:
            raise GateEvidenceError(f"Primary metric lacks observed evidence: {key}")
        if key == "cost_per_invoice_usd" and row.evidence_count != report.sample_count:
            raise GateEvidenceError("Cost evidence does not cover every invoice")
    return rows


def _tolerance(key: MetricKey) -> Decimal:
    if key in {"cost_per_invoice_usd", "p95_latency_seconds"}:
        return PRIMARY_TARGETS[key].value * RELATIVE_TOLERANCE
    return RATE_TOLERANCE


def compare_reports(
    baseline: PrimaryMetricsReport,
    candidate: PrimaryMetricsReport,
    *,
    baseline_sha256: str,
    candidate_sha256: str,
    expected_model_class: ModelClass = "openai-prod",
    expected_manifest_sha256: str | None = None,
) -> GateResult:
    before = _rows(baseline)
    after = _rows(candidate)
    if (
        baseline.model_class != candidate.model_class
        or candidate.model_class != expected_model_class
        or baseline.manifest_sha256 != candidate.manifest_sha256
        or (
            expected_manifest_sha256 is not None
            and candidate.manifest_sha256 != expected_manifest_sha256
        )
        or baseline.dataset_version != candidate.dataset_version
    ):
        raise GateEvidenceError("Reports differ in model class or golden manifest")
    assert candidate.model_class is not None
    comparison = []
    for key, target in PRIMARY_TARGETS.items():
        baseline_value = before[key].value
        candidate_value = after[key].value
        assert baseline_value is not None and candidate_value is not None
        delta = candidate_value - baseline_value
        degradation = -delta if target.direction == "min" else delta
        comparison.append(
            GateRow(
                key=key,
                baseline=baseline_value,
                candidate=candidate_value,
                delta=delta,
                tolerance=_tolerance(key),
                target=target.value,
                direction=target.direction,
                floor_breached=(
                    candidate_value < target.value
                    if target.direction == "min"
                    else candidate_value > target.value
                ),
                regression_breached=degradation > _tolerance(key),
            )
        )
    return GateResult(
        dataset_version=candidate.dataset_version,
        manifest_sha256=candidate.manifest_sha256,
        model_class=candidate.model_class,
        baseline_sha256=baseline_sha256,
        candidate_sha256=candidate_sha256,
        passed=not any(row.floor_breached or row.regression_breached for row in comparison),
        rows=tuple(comparison),
    )


def render_comment(result: GateResult) -> str:
    status = "PASS" if result.passed else "FAIL"
    lines = [
        f"### Golden evaluation gate: {status}",
        "",
        f"Model class: `{result.model_class}` · dataset: `{result.dataset_version}`",
        "",
        "| Primary metric | Main | PR | Δ (PR − main) | Target | Allowed regression | Result |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in result.rows:
        outcome = "FAIL" if row.floor_breached or row.regression_breached else "PASS"
        reasons = []
        if row.floor_breached:
            reasons.append("floor")
        if row.regression_breached:
            reasons.append("regression")
        suffix = f" ({', '.join(reasons)})" if reasons else ""
        comparator = "≥" if row.direction == "min" else "≤"
        lines.append(
            f"| `{row.key}` | {row.baseline} | {row.candidate} | {row.delta:+} "
            f"| {comparator} {row.target} | {row.tolerance} | {outcome}{suffix} |"
        )
    lines.extend(
        (
            "",
            "Rate tolerances are 0.5 percentage points; dollar and second tolerances "
            "are 0.5% of their versioned targets. Both reports must cover 500 invoices "
            "in three independent live runs.",
            "",
        )
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--comment-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--model-class", choices=("local-dev", "openai-prod"), default="openai-prod"
    )
    parser.add_argument("--manifest", type=Path, default=Path("eval/golden/v1.0.1/manifest.json"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        baseline_bytes = args.baseline.read_bytes()
        candidate_bytes = args.candidate.read_bytes()
        manifest_bytes = args.manifest.read_bytes()
        result = compare_reports(
            PrimaryMetricsReport.model_validate_json(baseline_bytes),
            PrimaryMetricsReport.model_validate_json(candidate_bytes),
            baseline_sha256=hashlib.sha256(baseline_bytes).hexdigest(),
            candidate_sha256=hashlib.sha256(candidate_bytes).hexdigest(),
            expected_model_class=args.model_class,
            expected_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        )
        args.comment_file.parent.mkdir(parents=True, exist_ok=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_new_artifact(args.comment_file, render_comment(result).encode())
        write_new_artifact(args.output, (result.model_dump_json(indent=2) + "\n").encode())
    except (GateEvidenceError, ValidationError, OSError, ValueError) as error:
        logger.error(
            "ci_gate_invalid_evidence error_type=%s message=%s", type(error).__name__, error
        )
        return 1
    logger.info("ci_gate_complete passed=%s model_class=%s", result.passed, result.model_class)
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

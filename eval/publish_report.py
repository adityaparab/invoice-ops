"""Publish measured golden metrics in the versioned Evals-screen contract."""

import argparse
import logging
from pathlib import Path

from pydantic import ValidationError

from eval.diagnostics import DiagnosticReport
from eval.metrics import PRIMARY_TARGETS, PrimaryMetric, PrimaryMetricsReport
from invoiceops_agent.api.schemas.evals import (
    AnomalyConfusion,
    EvalReport,
    MetricRow,
    TauSweepPoint,
)
from invoiceops_agent.artifacts import write_new_artifact

logger = logging.getLogger(__name__)


class PublicationEvidenceError(ValueError):
    """Metric and diagnostic reports cannot form one honest published summary."""


def _primary_row(metric: PrimaryMetric) -> MetricRow | None:
    if metric.value is None:
        return None
    unit = (
        "usd"
        if metric.key == "cost_per_invoice_usd"
        else "ms"
        if metric.key == "p95_latency_seconds"
        else "rate"
    )
    value = metric.value * 1000 if unit == "ms" else metric.value
    key = "p95_latency_ms" if unit == "ms" else metric.key
    return MetricRow(
        key=key,
        label=metric.key.replace("_", " ").capitalize(),
        scope="primary",
        value=value,
        unit=unit,
        sample_count=metric.sample_count,
    )


def publish_report(
    primary: PrimaryMetricsReport,
    diagnostics: DiagnosticReport,
    *,
    report_id: str,
    title: str,
) -> EvalReport:
    if (
        primary.mode != "live"
        or primary.model_class is None
        or diagnostics.mode != "live"
        or diagnostics.model_class != primary.model_class
        or diagnostics.manifest_sha256 != primary.manifest_sha256
        or diagnostics.dataset_version != primary.dataset_version
        or diagnostics.sample_count != primary.sample_count
    ):
        raise PublicationEvidenceError("Published sources differ in class, mode, or golden set")
    by_key = {row.key: row for row in primary.metrics}
    if (
        len(by_key) != len(primary.metrics)
        or set(by_key) != set(PRIMARY_TARGETS)
        or any(
            row.target != PRIMARY_TARGETS[key].value
            or row.direction != PRIMARY_TARGETS[key].direction
            for key, row in by_key.items()
        )
    ):
        raise PublicationEvidenceError("Primary metrics omit or change a versioned measure")
    caveats = list(dict.fromkeys((*primary.caveats, *diagnostics.caveats)))
    if not primary.complete_suite:
        caveats.append("Development or partial measurement; release floors were not validated.")
    missing = tuple(row.key for row in primary.metrics if row.value is None)
    if missing:
        caveats.append("Unavailable primary measures: " + ", ".join(missing) + ".")
    rows = [row for metric in primary.metrics if (row := _primary_row(metric)) is not None]
    rows.extend(
        MetricRow(
            key=f"{field.field}-f1",
            label=field.field.replace("_", " ").capitalize() + " F1",
            scope=f"tier {field.tier}",
            value=field.f1,
            unit="rate",
            sample_count=field.sample_count,
            tp=field.tp,
            fp=field.fp,
            fn=field.fn,
        )
        for field in diagnostics.field_by_tier
        if field.f1 is not None
    )
    sweep = [
        TauSweepPoint(
            threshold=point.threshold,
            exception_recall=point.routed_exception_recall,
            false_escalation_rate=point.false_escalation_rate,
            stp_rate=point.stp_rate,
        )
        for point in diagnostics.tau_sweep
        if point.routed_exception_recall is not None
        and point.false_escalation_rate is not None
        and point.stp_rate is not None
    ]
    if len(sweep) != len(diagnostics.tau_sweep):
        caveats.append("Threshold sweep lacks an eligible clean or anomalous denominator.")
    return EvalReport(
        report_id=report_id,
        report_version="pipeline-eval@v1",
        title=title,
        dataset_version=primary.dataset_version,
        measured_at=primary.scored_at,
        model_versions=list(primary.model_versions),
        metrics=rows,
        per_anomaly_confusion=[
            AnomalyConfusion(anomaly_code=row.code, tp=row.tp, fp=row.fp, fn=row.fn, tn=row.tn)
            for row in diagnostics.anomaly_confusion
        ],
        tau_sweep=sweep,
        caveats=caveats,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--diagnostics", type=Path, required=True)
    parser.add_argument("--report-id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        report = publish_report(
            PrimaryMetricsReport.model_validate_json(args.metrics.read_bytes()),
            DiagnosticReport.model_validate_json(args.diagnostics.read_bytes()),
            report_id=args.report_id,
            title=args.title,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_new_artifact(args.output, (report.model_dump_json(indent=2) + "\n").encode())
    except (PublicationEvidenceError, ValidationError, OSError, ValueError) as error:
        logger.error("eval_publication_failed error_type=%s", type(error).__name__)
        return 1
    logger.info("eval_publication_written output=%s", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

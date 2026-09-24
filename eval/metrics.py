"""Primary, exact-value metrics over auditable golden pipeline reports."""

import argparse
import hashlib
import logging
import math
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, NamedTuple

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from eval.golden.schema import GoldenManifest, GoldenSample, InvoiceLabel
from eval.runners.schema import ModelClass, PipelineReport, RunRecord
from invoiceops_agent.artifacts import write_new_artifact
from invoiceops_agent.schemas.extraction import InvoiceExtraction

MetricKey = Literal[
    "exception_recall",
    "false_escalation_rate",
    "field_f1",
    "money_field_f1",
    "routing_accuracy",
    "stp_rate",
    "cost_per_invoice_usd",
    "p95_latency_seconds",
]
logger = logging.getLogger(__name__)


class MetricTarget(NamedTuple):
    direction: Literal["min", "max"]
    value: Decimal


PRIMARY_TARGETS: Mapping[MetricKey, MetricTarget] = {
    "exception_recall": MetricTarget("min", Decimal("0.98")),
    "false_escalation_rate": MetricTarget("max", Decimal("0.05")),
    "field_f1": MetricTarget("min", Decimal("0.95")),
    "money_field_f1": MetricTarget("min", Decimal("0.97")),
    "routing_accuracy": MetricTarget("min", Decimal("0.95")),
    "stp_rate": MetricTarget("min", Decimal("0.70")),
    "cost_per_invoice_usd": MetricTarget("max", Decimal("0.04")),
    "p95_latency_seconds": MetricTarget("max", Decimal(45)),
}

HEADER_FIELDS = (
    "vendor_name",
    "vendor_tax_id",
    "bank_account_iban",
    "invoice_number",
    "po_number",
    "currency",
    "invoice_date",
    "due_date",
    "subtotal",
    "tax_amount",
    "total_amount",
)
LINE_FIELDS = ("description", "quantity", "unit_price", "tax_rate", "line_total")
MONEY_FIELDS = frozenset({"subtotal", "tax_amount", "total_amount", "unit_price", "line_total"})
NUMERIC_FIELDS = frozenset(
    {"subtotal", "tax_amount", "total_amount", "quantity", "unit_price", "tax_rate", "line_total"}
)


class MetricEvidenceError(ValueError):
    """The supplied reports cannot be compared with the committed golden labels."""


class PrimaryMetric(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    key: MetricKey
    target: Decimal
    direction: Literal["min", "max"]
    value: Decimal | None = None
    numerator: int | None = Field(default=None, ge=0)
    denominator: int | None = Field(default=None, ge=0)
    sample_count: int = Field(ge=0)
    evidence_count: int = Field(ge=0)


class PrimaryMetricsReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    version: Literal["primary-metrics@v1", "primary-metrics@v2"] = "primary-metrics@v2"
    dataset_version: Literal["golden/v1.0.0", "golden/v1.0.1"] = "golden/v1.0.1"
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: Literal["live", "recorded"]
    model_class: ModelClass | None = None
    model_versions: tuple[str, ...] = Field(default=(), max_length=50)
    report_count: int = Field(ge=1, le=3)
    sample_count: int = Field(ge=1, le=500)
    complete_suite: bool
    scored_at: AwareDatetime
    metrics: tuple[PrimaryMetric, ...]
    caveats: tuple[str, ...] = ()


@dataclass(frozen=True)
class FieldCounts:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    def __add__(self, other: "FieldCounts") -> "FieldCounts":
        return FieldCounts(self.tp + other.tp, self.fp + other.fp, self.fn + other.fn)

    @property
    def f1(self) -> Decimal | None:
        return _f1(self.tp, self.fp, self.fn)


def _text(value: object) -> str | None:
    if value is None:
        return None
    return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def _normalized(field: str, value: object) -> str | None:
    if value is None:
        return None
    if field == "bank_account_iban":
        return "".join(str(value).split()).upper()
    if field in NUMERIC_FIELDS:
        try:
            amount = Decimal(str(value).strip())
        except InvalidOperation:
            return None
        return format(amount.normalize(), "f") if amount.is_finite() else None
    if field in {"invoice_date", "due_date"}:
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    return _text(value)


def _extraction(record: RunRecord) -> InvoiceExtraction | None:
    payload = record.detail.evidence.get("extraction.completed")
    if not payload:
        return None
    result = payload.get("result")
    if not isinstance(result, dict) or result.get("status") != "EXTRACTED":
        return None
    raw = result.get("extraction")
    return InvoiceExtraction.model_validate(raw) if isinstance(raw, dict) else None


def score_fields(
    label: InvoiceLabel, extraction: InvoiceExtraction | None
) -> dict[str, FieldCounts]:
    """Count exact matches by known field without treating unknown labels as negatives."""
    results = {field: FieldCounts() for field in HEADER_FIELDS}
    results.update({f"line_{field}": FieldCounts() for field in LINE_FIELDS})

    def compare(field: str, expected: object, observed: object) -> FieldCounts:
        if expected is None:
            return FieldCounts()
        actual = _normalized(field, observed)
        if _normalized(field, expected) == actual:
            return FieldCounts(tp=1)
        return FieldCounts(fp=int(actual is not None), fn=1)

    for field in HEADER_FIELDS:
        observed = getattr(extraction, field).value if extraction is not None else None
        results[field] += compare(field, getattr(label, field), observed)
    observed_lines = extraction.line_items if extraction is not None else ()
    for index, line in enumerate(label.line_items):
        actual = observed_lines[index] if index < len(observed_lines) else None
        for field in LINE_FIELDS:
            observed = getattr(actual, field).value if actual is not None else None
            results[f"line_{field}"] += compare(field, getattr(line, field), observed)
    if label.line_items:
        for extra_line in observed_lines[len(label.line_items) :]:
            for field in LINE_FIELDS:
                if getattr(extra_line, field).value is not None:
                    results[f"line_{field}"] += FieldCounts(fp=1)
    return results


def _field_counts(label: InvoiceLabel, extraction: InvoiceExtraction | None) -> Counter[str]:
    counts: Counter[str] = Counter()
    for field, row in score_fields(label, extraction).items():
        counts.update({"tp": row.tp, "fp": row.fp, "fn": row.fn})
        if field in MONEY_FIELDS or field.startswith("line_") and field[5:] in MONEY_FIELDS:
            counts.update({"money_tp": row.tp, "money_fp": row.fp, "money_fn": row.fn})
    return counts


def _f1(tp: int, fp: int, fn: int) -> Decimal | None:
    denominator = 2 * tp + fp + fn
    return Decimal(2 * tp) / denominator if denominator else None


def _metric(
    key: MetricKey,
    *,
    value: Decimal | None,
    sample_count: int,
    evidence_count: int,
    numerator: int | None = None,
    denominator: int | None = None,
) -> PrimaryMetric:
    target = PRIMARY_TARGETS[key]
    return PrimaryMetric(
        key=key,
        target=target.value,
        direction=target.direction,
        value=value,
        sample_count=sample_count,
        evidence_count=evidence_count,
        numerator=numerator,
        denominator=denominator,
    )


def _rate(
    key: MetricKey, numerator: int, denominator: int, *, evidence_count: int | None = None
) -> PrimaryMetric:
    return _metric(
        key,
        value=Decimal(numerator) / denominator if denominator else None,
        numerator=numerator,
        denominator=denominator,
        sample_count=denominator,
        evidence_count=denominator if evidence_count is None else evidence_count,
    )


def _codes(record: RunRecord) -> set[str]:
    payload = record.detail.evidence.get("classification.completed")
    raw = payload.get("codes") if payload is not None else None
    return {item for item in raw if isinstance(item, str)} if isinstance(raw, list) else set()


def _correct_route(sample: GoldenSample, record: RunRecord) -> bool:
    if not sample.anomaly_codes:
        return _auto_approved(record)
    if "DUP_EXACT" in sample.anomaly_codes:
        return record.upload.duplicate and record.route == "REJECT"
    return record.route == "REVIEW" and record.detail.invoice.status == "NEEDS_REVIEW"


def _cost_amount(value: object) -> Decimal | None:
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def _auto_approved(record: RunRecord) -> bool:
    events = {event.event_type for event in record.provenance.events}
    return (
        record.route == "AUTO_APPROVE"
        and record.detail.invoice.status == "APPROVED"
        and "approval.auto_granted" in events
        and not any(event.startswith("review.") for event in events)
    )


def _cost(record: RunRecord) -> Decimal | None:
    if record.upload.duplicate:
        return Decimal(0)
    if record.worker_error_type or record.replayed_before_worker or record.route == "NONE":
        return None
    evidence = record.detail.evidence
    extraction_event = evidence.get("extraction.completed") or evidence.get("extraction.escalated")
    if extraction_event is None:
        return None
    result = extraction_event.get("result")
    if not isinstance(result, dict):
        return None
    calls = result.get("calls")
    if not isinstance(calls, list):
        return None
    amounts: list[Decimal] = []
    for call in calls:
        if not isinstance(call, dict) or call.get("cost_usd") is None:
            return None
        amount = _cost_amount(call["cost_usd"])
        if amount is None:
            return None
        amounts.append(amount)
    if result.get("status") == "EXTRACTED":
        similarity = evidence.get("similarity.completed")
        if similarity is None or similarity.get("gateway_cost_usd") is None:
            return None
        amount = _cost_amount(similarity["gateway_cost_usd"])
        if amount is None:
            return None
        amounts.append(amount)
    triage = evidence.get("triage.prepared")
    if record.route == "REVIEW" and triage is None:
        return None
    if triage is not None:
        payload = triage.get("triage")
        if not isinstance(payload, dict) or payload.get("cost_usd") is None:
            return None
        amount = _cost_amount(payload["cost_usd"])
        if amount is None:
            return None
        amounts.append(amount)
    return sum(amounts, Decimal(0))


def _auto_latency(record: RunRecord) -> Decimal | None:
    if not _auto_approved(record):
        return None
    starts = [
        event.created_at
        for event in record.provenance.events
        if event.event_type == "ingest.accepted"
    ]
    ends = [
        event.created_at
        for event in record.provenance.events
        if event.event_type == "approval.auto_granted"
    ]
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        return None
    delta = ends[0] - starts[0]
    return Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / 1_000_000


def _p95(values: Iterable[Decimal]) -> Decimal | None:
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * 0.95) - 1] if ordered else None


def score_primary_metrics(
    manifest: GoldenManifest,
    manifest_sha256: str,
    reports: tuple[PipelineReport, ...],
) -> PrimaryMetricsReport:
    if not 1 <= len(reports) <= 3:
        raise MetricEvidenceError("Supply one to three pipeline reports")
    ids = {sample.sample_id for sample in manifest.samples}
    first = reports[0]
    selected = {record.sample_id for record in first.samples}
    if not selected or not selected <= ids or len(selected) != len(first.samples):
        raise MetricEvidenceError("Report sample IDs are empty, repeated, or outside the manifest")
    if any(
        report.manifest_sha256 != manifest_sha256
        or report.dataset_version != manifest.version
        or report.mode != first.mode
        or report.model_class != first.model_class
        or {record.sample_id for record in report.samples} != selected
        or len(report.samples) != len(selected)
        for report in reports
    ):
        raise MetricEvidenceError("Pipeline reports differ in manifest, mode, or selected samples")
    labels: Mapping[str, GoldenSample] = {sample.sample_id: sample for sample in manifest.samples}
    for report in reports:
        for record in report.samples:
            sample = labels[record.sample_id]
            if (
                record.split != sample.split
                or record.expected_codes != sample.anomaly_codes
                or record.document_sha256 != sample.document_sha256
                or record.upload.duplicate != ("DUP_EXACT" in sample.anomaly_codes)
            ):
                raise MetricEvidenceError("Pipeline sample evidence differs from its golden label")
    records = {record.sample_id: record for record in first.samples}
    clean = [
        sample
        for sample in manifest.samples
        if sample.sample_id in selected and sample.routing_eligible and not sample.anomaly_codes
    ]
    anomalies = [
        sample
        for sample in manifest.samples
        if sample.sample_id in selected and sample.routing_eligible and sample.anomaly_codes
    ]
    routing = [*clean, *anomalies]
    recalled = 0
    classified = 0
    for sample in anomalies:
        record = records[sample.sample_id]
        codes = _codes(record)
        if record.upload.duplicate:
            codes.add("DUP_EXACT")
        if record.upload.duplicate or "classification.completed" in record.detail.evidence:
            classified += 1
        recalled += set(sample.anomaly_codes) <= codes
    false_escalations = sum(not _auto_approved(records[sample.sample_id]) for sample in clean)
    stp = sum(_auto_approved(records[sample.sample_id]) for sample in clean)
    correct_routes = sum(_correct_route(sample, records[sample.sample_id]) for sample in routing)
    field_counts: Counter[str] = Counter()
    field_samples = 0
    extracted_samples = 0
    for sample_id, record in records.items():
        if record.upload.duplicate:
            continue
        field_samples += 1
        extraction = _extraction(record)
        extracted_samples += extraction is not None
        field_counts.update(_field_counts(labels[sample_id].label, extraction))
    field_tp, field_fp, field_fn = (field_counts[key] for key in ("tp", "fp", "fn"))
    money_tp, money_fp, money_fn = (
        field_counts[key] for key in ("money_tp", "money_fp", "money_fn")
    )
    costs = [_cost(record) for record in first.samples]
    observed_costs = [cost for cost in costs if cost is not None]
    if any(cost < 0 for cost in observed_costs):
        raise MetricEvidenceError("Gateway cost cannot be negative")
    cost_value = (
        sum(observed_costs, Decimal(0)) / len(first.samples)
        if len(observed_costs) == len(first.samples)
        else None
    )
    valid_latency_runs = (
        len(reports) == 3
        and first.mode == "live"
        and len({report.started_at for report in reports}) == 3
        and len(
            {
                record.upload.run_id
                for report in reports
                for record in report.samples
                if not record.upload.duplicate
            }
        )
        == sum(not record.upload.duplicate for report in reports for record in report.samples)
        and all(
            not record.worker_error_type and not record.replayed_before_worker
            for report in reports
            for record in report.samples
        )
    )
    latency_rows = (
        [
            _auto_latency(record)
            for report in reports
            for record in report.samples
            if labels[record.sample_id].routing_eligible
            and not labels[record.sample_id].anomaly_codes
            and _auto_approved(record)
        ]
        if valid_latency_runs
        else []
    )
    latency = _p95(value for value in latency_rows if value is not None)
    if any(value is None for value in latency_rows) or not all(
        any(_auto_approved(record) for record in report.samples) for report in reports
    ):
        latency = None
    metrics = (
        _rate("exception_recall", recalled, len(anomalies), evidence_count=classified),
        _rate("false_escalation_rate", false_escalations, len(clean)),
        _metric(
            "field_f1",
            value=_f1(field_tp, field_fp, field_fn),
            numerator=2 * field_tp,
            denominator=2 * field_tp + field_fp + field_fn,
            sample_count=field_samples,
            evidence_count=extracted_samples,
        ),
        _metric(
            "money_field_f1",
            value=_f1(money_tp, money_fp, money_fn),
            numerator=2 * money_tp,
            denominator=2 * money_tp + money_fp + money_fn,
            sample_count=field_samples,
            evidence_count=extracted_samples,
        ),
        _rate("routing_accuracy", correct_routes, len(routing)),
        _rate("stp_rate", stp, len(clean)),
        _metric(
            "cost_per_invoice_usd",
            value=cost_value,
            sample_count=len(first.samples),
            evidence_count=len(observed_costs),
        ),
        _metric(
            "p95_latency_seconds",
            value=latency,
            sample_count=len(clean) * len(reports),
            evidence_count=len(latency_rows) - latency_rows.count(None),
        ),
    )
    caveats = []
    if first.mode == "recorded":
        caveats.append(
            "Recorded cassettes are pipeline smoke evidence, not model quality evidence."
        )
    if first.mode == "live" and first.model_class is None:
        caveats.append("Historical live report has no declared model class.")
    if len(selected) != len(ids):
        caveats.append("Selected samples do not cover the full 500-invoice golden suite.")
    if len(observed_costs) != len(first.samples):
        caveats.append("Gateway cost is unavailable for at least one invoice.")
    if latency is None:
        caveats.append(
            "P95 requires three independent live runs with auditable auto-approve timestamps."
        )
    return PrimaryMetricsReport(
        version="primary-metrics@v2" if first.model_class else "primary-metrics@v1",
        dataset_version=manifest.version,
        manifest_sha256=manifest_sha256,
        mode=first.mode,
        model_class=first.model_class,
        model_versions=tuple(
            sorted(
                {
                    event.versions.model_version
                    for report in reports
                    for record in report.samples
                    for event in record.provenance.events
                    if event.event_type
                    in {
                        "extraction.completed",
                        "extraction.escalated",
                        "similarity.completed",
                        "triage.prepared",
                    }
                }
            )
        ),
        report_count=len(reports),
        sample_count=len(selected),
        complete_suite=(
            len(selected) == len(ids)
            and first.mode == "live"
            and first.model_class is not None
            and all(
                not record.worker_error_type and not record.replayed_before_worker
                for report in reports
                for record in report.samples
            )
        ),
        scored_at=max(report.completed_at for report in reports),
        metrics=metrics,
        caveats=tuple(caveats),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("eval/golden/v1.0.1/manifest.json"))
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        manifest_bytes = args.manifest.read_bytes()
        manifest = GoldenManifest.model_validate_json(manifest_bytes)
        reports = tuple(
            PipelineReport.model_validate_json(path.read_bytes()) for path in args.input
        )
        scored = score_primary_metrics(
            manifest, hashlib.sha256(manifest_bytes).hexdigest(), reports
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_new_artifact(args.output, (scored.model_dump_json(indent=2) + "\n").encode())
    except (MetricEvidenceError, ValidationError, OSError, ValueError) as error:
        logger.error("primary_metrics_failed error_type=%s", type(error).__name__)
        return 1
    logger.info(
        "primary_metrics_written output=%s complete_suite=%s",
        args.output,
        scored.complete_suite,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

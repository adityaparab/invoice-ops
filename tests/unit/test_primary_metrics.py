"""Primary scores use known labels, immutable evidence, and complete cost coverage."""

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from eval.golden.schema import GoldenSample
from eval.metrics import MetricEvidenceError, _field_counts, score_primary_metrics
from eval.runners.build_smoke_cassettes import _extraction
from eval.runners.run_pipeline import load_manifest
from eval.runners.schema import PipelineReport, RunRecord

from invoiceops_agent.api.schemas.invoice_read import InvoiceDetail, InvoiceSummary
from invoiceops_agent.api.schemas.provenance import InvoiceProvenancePage
from invoiceops_agent.ledger.schemas import LedgerEvent
from invoiceops_agent.tools.ingestion_schemas import IngestionResult

pytestmark = pytest.mark.unit
MANIFEST, MANIFEST_SHA = load_manifest()
START = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)


def _sample(*, code: str | None = None) -> GoldenSample:
    return next(
        sample
        for sample in MANIFEST.samples
        if sample.routing_eligible
        and (sample.anomaly_codes == (code,) if code is not None else not sample.anomaly_codes)
    )


def _record(
    sample: GoldenSample,
    *,
    route: str,
    code: str | None = None,
    elapsed_seconds: int = 5,
    embedding_cost: str | None = "0.002",
    run_number: int = 0,
) -> RunRecord:
    duplicate = "DUP_EXACT" in sample.anomaly_codes
    identity = hashlib.sha256(f"{sample.sample_id}:{run_number}".encode()).digest()
    invoice_id = UUID(bytes=identity[:16])
    run_id = UUID(bytes=identity[16:])
    evidence: dict[str, dict[str, object]] = {}
    if not duplicate:
        evidence = {
            "extraction.completed": {
                "result": {
                    "status": "EXTRACTED",
                    "extraction": _extraction(sample.label).model_dump(mode="json"),
                    "calls": [{"cost_usd": "0.001"}],
                }
            },
            "similarity.completed": {"gateway_cost_usd": embedding_cost},
            "classification.completed": {"codes": [code] if code else []},
        }
        if route == "REVIEW":
            evidence["triage.prepared"] = {"triage": {"cost_usd": "0.003"}}
    status = "ARCHIVED" if route == "ARCHIVE" else "NEEDS_REVIEW"
    events = [LedgerEvent.model_construct(event_type="ingest.accepted", created_at=START)]
    if route == "ARCHIVE":
        events.append(
            LedgerEvent.model_construct(
                event_type="approval.auto_granted",
                created_at=START + timedelta(seconds=elapsed_seconds),
            )
        )
    return RunRecord.model_construct(
        sample_id=sample.sample_id,
        split=sample.split,
        expected_codes=sample.anomaly_codes,
        document_sha256=sample.document_sha256,
        upload=IngestionResult(invoice_id=invoice_id, run_id=run_id, duplicate=duplicate),
        upload_duration_ms=5.0,
        worker_duration_ms=None if duplicate else 25.0,
        replayed_before_worker=False,
        route=route,
        worker_error_type=None,
        detail=InvoiceDetail.model_construct(
            invoice=InvoiceSummary.model_construct(id=invoice_id, status=status),
            evidence=evidence,
        ),
        provenance=InvoiceProvenancePage.model_construct(events=events),
    )


def _report(
    records: tuple[RunRecord, ...], *, offset_minutes: int, mode: str = "live"
) -> PipelineReport:
    started = START + timedelta(minutes=offset_minutes)
    return PipelineReport.model_construct(
        mode=mode,
        manifest_sha256=MANIFEST_SHA,
        started_at=started,
        completed_at=started + timedelta(minutes=1),
        samples=records,
    )


def _metrics(report: PipelineReport, *other: PipelineReport) -> dict[str, Decimal | None]:
    scored = score_primary_metrics(MANIFEST, MANIFEST_SHA, (report, *other))
    return {metric.key: metric.value for metric in scored.metrics}


def test_primary_rates_cost_and_three_run_event_latency() -> None:
    clean = _sample()
    anomaly = _sample(code="PRICE_MM")
    duplicate = _sample(code="DUP_EXACT")
    reports = tuple(
        _report(
            (
                _record(clean, route="ARCHIVE", elapsed_seconds=latency, run_number=index),
                _record(anomaly, route="REVIEW", code="PRICE_MM", run_number=index),
                _record(duplicate, route="REJECT", run_number=index),
            ),
            offset_minutes=index,
        )
        for index, latency in enumerate((5, 10, 20))
    )
    values = _metrics(*reports)
    assert values == {
        "exception_recall": Decimal(1),
        "false_escalation_rate": Decimal(0),
        "field_f1": Decimal(1),
        "money_field_f1": Decimal(1),
        "routing_accuracy": Decimal(1),
        "stp_rate": Decimal(1),
        "cost_per_invoice_usd": Decimal("0.003"),
        "p95_latency_seconds": Decimal(20),
    }
    scored = score_primary_metrics(MANIFEST, MANIFEST_SHA, reports)
    assert not scored.complete_suite
    assert scored.sample_count == 3
    assert scored.metrics[0].evidence_count == 2
    assert next(metric for metric in scored.metrics if metric.key == "field_f1").sample_count == 2
    assert next(metric for metric in scored.metrics if metric.key == "field_f1").evidence_count == 2
    assert next(metric for metric in scored.metrics if metric.key == "stp_rate").target == Decimal(
        "0.70"
    )


def test_missed_code_reviewed_clean_and_missing_cost_are_explicit() -> None:
    clean = _sample()
    anomaly = _sample(code="PRICE_MM")
    report = _report(
        (
            _record(clean, route="REVIEW", embedding_cost=None),
            _record(anomaly, route="REVIEW", code=None),
        ),
        offset_minutes=0,
    )
    values = _metrics(report)
    assert values["exception_recall"] == 0
    assert values["false_escalation_rate"] == 1
    assert values["stp_rate"] == 0
    assert values["routing_accuracy"] == Decimal("0.5")
    assert values["cost_per_invoice_usd"] is None
    assert values["p95_latency_seconds"] is None


def test_unknown_labels_are_skipped_and_wrong_values_count_fp_and_fn() -> None:
    sample = next(sample for sample in MANIFEST.samples if sample.origin == "voxel51")
    extraction = _extraction(sample.label)
    scores = _field_counts(sample.label, extraction)
    assert scores["fp"] == scores["fn"] == 0
    assert scores["tp"] > 0
    assert sample.label.bank_account_iban is None
    wrong = extraction.model_copy(
        update={
            "vendor_name": extraction.vendor_name.model_copy(update={"value": "Wrong supplier"})
        }
    )
    changed = _field_counts(sample.label, wrong)
    assert changed["fp"] == changed["fn"] == 1
    missing = _field_counts(sample.label, None)
    assert missing["tp"] == 0 and missing["fn"] > 0


def test_recorded_mode_and_mismatched_manifests_cannot_pose_as_full_measurement() -> None:
    sample = _sample()
    report = _report((_record(sample, route="ARCHIVE"),), offset_minutes=0, mode="recorded")
    scored = score_primary_metrics(MANIFEST, MANIFEST_SHA, (report,))
    assert not scored.complete_suite
    assert (
        next(metric for metric in scored.metrics if metric.key == "p95_latency_seconds").value
        is None
    )
    with pytest.raises(MetricEvidenceError, match="differ"):
        score_primary_metrics(MANIFEST, "0" * 64, (report,))
    forged_record = report.samples[0].model_copy(update={"document_sha256": "0" * 64})
    forged = report.model_copy(update={"samples": (forged_record,)})
    with pytest.raises(MetricEvidenceError, match="golden label"):
        score_primary_metrics(MANIFEST, MANIFEST_SHA, (forged,))

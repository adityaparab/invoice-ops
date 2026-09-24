"""The API runner preflights gold bytes and never needs network in unit tests."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from eval.golden.schema import GoldenSample
from eval.runners.build_smoke_cassettes import OUTPUT, record
from eval.runners.run_pipeline import (
    PipelineRunError,
    load_manifest,
    preflight_documents,
    run_pipeline,
    select_samples,
)

from invoiceops_agent.api.schemas.invoice_read import InvoiceDetail, InvoiceSummary
from invoiceops_agent.api.schemas.provenance import InvoiceProvenancePage
from invoiceops_agent.graph import batch_cli
from invoiceops_agent.graph.batch_cli import parse_run_ids
from invoiceops_agent.tools.ingestion_schemas import IngestionResult

pytestmark = pytest.mark.unit


class FakeAPI:
    def __init__(self) -> None:
        self.uploaded: list[str] = []
        self.detail_reads = 0

    def ready(self) -> None:
        pass

    def upload(self, sample: GoldenSample, body: bytes) -> IngestionResult:
        self.uploaded.append(sample.sample_id)
        assert body.startswith(b"\x89PNG\r\n\x1a\n")
        return IngestionResult(invoice_id=UUID(int=2), run_id=UUID(int=1))

    def detail(self, invoice_id: UUID) -> InvoiceDetail:
        self.detail_reads += 1
        timestamp = datetime(2026, 8, 27, tzinfo=UTC)
        return InvoiceDetail(
            invoice=InvoiceSummary(
                id=invoice_id,
                run_id=UUID(int=1),
                status="QUEUED" if self.detail_reads == 1 else "ARCHIVED",
                run_status="QUEUED" if self.detail_reads == 1 else "COMPLETED",
                source="UPLOAD",
                content_type="image/png",
                created_at=timestamp,
            ),
            exception=None,
            evidence={},
            read_at=timestamp,
        )

    def provenance(self, invoice_id: UUID) -> InvoiceProvenancePage:
        return InvoiceProvenancePage(
            invoice_id=invoice_id,
            status="ARCHIVED",
            source="UPLOAD",
            created_at=datetime(2026, 8, 27, tzinfo=UTC),
            events=[],
            next_cursor=None,
        )


class FakeWorker:
    def __init__(self, *, expected_recorded: bool = True) -> None:
        self.run_ids: tuple[UUID, ...] = ()
        self.expected_recorded = expected_recorded

    def process(self, run_ids: tuple[UUID, ...], *, recorded: bool) -> dict[UUID, dict[str, str]]:
        self.run_ids = run_ids
        assert recorded == self.expected_recorded
        return {run_id: {"run_id": str(run_id), "route": "ARCHIVE"} for run_id in run_ids}


def test_recorded_mode_reconstructs_one_development_document_and_uses_real_boundaries() -> None:
    manifest, digest = load_manifest()
    selected = select_samples(manifest, recorded=True)
    documents = preflight_documents(Path("unused"), selected, recorded=True)
    api, worker = FakeAPI(), FakeWorker()
    report = run_pipeline(manifest, digest, selected, documents, api, worker, recorded=True)
    assert len(report.samples) == 1
    assert report.samples[0].route == "ARCHIVE"
    assert report.samples[0].upload.run_id == UUID(int=1)
    assert not report.samples[0].replayed_before_worker
    assert worker.run_ids == (UUID(int=1),)
    assert api.uploaded == [selected[0].sample_id]


def test_model_class_is_required_before_live_pipeline_side_effects() -> None:
    manifest, digest = load_manifest()
    selected = select_samples(manifest, recorded=True)
    documents = preflight_documents(Path("unused"), selected, recorded=True)
    api, worker = FakeAPI(), FakeWorker()
    with pytest.raises(PipelineRunError, match="model class"):
        run_pipeline(manifest, digest, selected, documents, api, worker, recorded=False)
    assert api.uploaded == [] and worker.run_ids == ()
    live = run_pipeline(
        manifest,
        digest,
        selected,
        documents,
        api,
        FakeWorker(expected_recorded=False),
        recorded=False,
        model_class="local-dev",
    )
    assert live.version == "pipeline-run@v2" and live.model_class == "local-dev"


def test_selected_duplicate_cannot_lose_its_parent() -> None:
    manifest, _ = load_manifest()
    duplicate = next(
        sample for sample in manifest.samples if sample.anomaly_codes == ("DUP_EXACT",)
    )
    selected = select_samples(manifest, split=duplicate.split)
    assert duplicate.parent_id in {sample.sample_id for sample in selected}
    misordered = manifest.model_copy(
        update={
            "samples": (duplicate, *(sample for sample in manifest.samples if sample != duplicate))
        }
    )
    with pytest.raises(PipelineRunError, match="parent"):
        select_samples(misordered, split=duplicate.split, limit=1)


def test_batch_rejects_invalid_or_repeated_ids() -> None:
    value = str(UUID(int=1))
    assert parse_run_ids(value + "\n") == (UUID(int=1),)
    with pytest.raises(ValueError, match="unique"):
        parse_run_ids(value + "\n" + value + "\n")
    with pytest.raises(ValueError, match="invalid"):
        parse_run_ids("not-a-uuid\n")


@pytest.mark.asyncio
async def test_batch_worker_reports_each_failure_without_skipping_later_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_invoice(run_id: UUID, *, gateway_transport: object = None) -> dict[str, str]:
        if run_id == UUID(int=1):
            raise RuntimeError("synthetic failure")
        return {"run_id": str(run_id), "route": "ARCHIVE"}

    monkeypatch.setattr(batch_cli, "run_invoice", fake_run_invoice)
    results = await batch_cli.run_batch((UUID(int=1), UUID(int=2)))
    assert [row["run_id"] for row in results] == [str(UUID(int=1)), str(UUID(int=2))]
    assert results[0]["error_type"] == "RuntimeError"
    assert results[1]["route"] == "ARCHIVE"
    assert all(float(row["duration_ms"]) >= 0 for row in results)


def test_synthetic_smoke_cassettes_reproduce_byte_for_byte(tmp_path: Path) -> None:
    asyncio.run(record(tmp_path))
    names = {path.name for path in tmp_path.glob("*.json")}
    assert len(names) == 3
    for name in names:
        assert (tmp_path / name).read_bytes() == (OUTPUT / name).read_bytes()

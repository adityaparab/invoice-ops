"""Run the existing extraction agent on the pinned development subset through LiteLLM."""

import argparse
import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ValidationError

from eval.baseline.metrics import (
    SCORING_VERSION,
    Counts,
    Manifest,
    Sample,
    Tier,
    score_sample,
    summarize,
)
from eval.baseline.settings import BaselineSettings
from invoiceops_agent.agents.extraction import ExtractionAgent, ExtractionGateway
from invoiceops_agent.artifacts import write_new_artifact
from invoiceops_agent.gateway_client import GatewayClient
from invoiceops_agent.ledger.schemas import AppendEvent, LedgerEvent, VersionPins
from invoiceops_agent.obs.logging import configure_logging
from invoiceops_agent.schemas.documents import DocumentReference
from invoiceops_agent.schemas.extraction import (
    ExtractionEscalation,
    ExtractionRequest,
    ExtractionSuccess,
)
from invoiceops_agent.tools.document_preflight import DocumentPreflight
from invoiceops_agent.tools.document_settings import DocumentSettings
from invoiceops_agent.tools.ingestion_schemas import RawDocument

logger = logging.getLogger(__name__)
REFERENCE_REPORT = Path("eval/reports/voxel51-v1.json")
DEFAULT_DATASET = Path("eval/data/voxel51-v1")
DEFAULT_OUTPUT = Path("eval/reports/extraction-baseline-v1.json")


@dataclass
class PreparedReader:
    document: RawDocument

    async def read(
        self, reference: DocumentReference, *, run_id: UUID, trace_id: str
    ) -> RawDocument:
        return self.document


@dataclass
class BaselineAuditSink:
    """Capture the agent's audit event in this disposable evaluation run."""

    events: list[AppendEvent] = field(default_factory=list)

    async def append(self, command: AppendEvent, *, trace_id: str) -> LedgerEvent:
        if command.versions is None or command.versions.model_version is None:
            raise ValueError("Extraction audit must pin its model version")
        if command.versions.prompt_version is None:
            raise ValueError("Extraction audit must pin its prompt version")
        self.events.append(command)
        return LedgerEvent(
            **command.model_dump(exclude={"versions"}),
            id=uuid5(NAMESPACE_URL, f"baseline-event:{command.run_id}"),
            sequence=1,
            created_at=datetime.now(UTC),
            versions=VersionPins(
                graph_version="baseline-eval@v1",
                model_version=command.versions.model_version,
                prompt_version=command.versions.prompt_version,
                policy_version="not-applicable@v1",
            ),
        )


def _load_manifest(dataset: Path, reference_report: Path) -> tuple[Manifest, str]:
    manifest_path = dataset / "manifest.json"
    raw = manifest_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    report = json.loads(reference_report.read_text(encoding="utf-8"))
    if digest != report["manifest_sha256"]:
        raise ValueError("Dataset manifest differs from its committed preparation report")
    manifest = Manifest.model_validate_json(raw)
    if len(manifest.samples) != report["settings"]["count"]:
        raise ValueError("Dataset sample count differs from its preparation report")
    return manifest, digest


def _read_prepared(dataset: Path, relative: Path) -> bytes:
    path = (dataset / relative).resolve()
    if not path.is_relative_to(dataset.resolve()):
        raise ValueError("Prepared image path escapes the dataset")
    return path.read_bytes()


async def _document(dataset: Path, sample: Sample) -> RawDocument:
    relative = Path(sample.prepared_path)
    if relative.is_absolute() or len(relative.parts) != 2 or relative.parts[0] != "prepared":
        raise ValueError("Prepared image path is outside the dataset")
    body = await asyncio.to_thread(_read_prepared, dataset, relative)
    if hashlib.sha256(body).hexdigest() != sample.prepared_sha256:
        raise ValueError("Prepared image checksum differs from the manifest")
    if len(body) > 5_000_000 or not body.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Prepared image exceeds gateway limits or is not PNG")
    return RawDocument(
        content_type="image/png",
        content_hash=sample.prepared_sha256,
        request_hash="0" * 64,
        body=body,
    )


def _request(sample: Sample, document: RawDocument) -> ExtractionRequest:
    return ExtractionRequest(
        run_id=uuid5(NAMESPACE_URL, f"baseline-run:{sample.sample_id}"),
        invoice_id=uuid5(NAMESPACE_URL, f"baseline-invoice:{sample.sample_id}"),
        trace_id=hashlib.sha256(sample.sample_id.encode("ascii")).hexdigest()[:32],
        raw_ref=f"s3://baseline/{document.object_key}",
        content_hash=document.content_hash,
        content_type=document.content_type,
        scenario=f"voxel51_{sample.sample_id}",
    )


async def run_baseline(
    *,
    dataset: Path,
    reference_report: Path,
    output: Path,
    gateway: ExtractionGateway,
) -> dict[str, object]:
    manifest, manifest_hash = _load_manifest(dataset, reference_report)
    for sample in manifest.samples:
        score_sample(sample, None)
        await _document(dataset, sample)
    preflight = DocumentPreflight(DocumentSettings())
    scores: list[tuple[Tier, dict[str, Counts]]] = []
    samples: list[dict[str, object]] = []
    escalations: dict[str, int] = {}
    input_tokens = output_tokens = 0
    cost = Decimal(0)
    complete_cost = True
    model_versions: set[str] = set()
    prompt_versions: set[str] = set()
    for index, sample in enumerate(manifest.samples, start=1):
        document = await _document(dataset, sample)
        audit = BaselineAuditSink()
        result = await ExtractionAgent(PreparedReader(document), preflight, gateway, audit).extract(
            _request(sample, document)
        )
        if len(audit.events) != 1:
            raise ValueError("Every baseline extraction must have exactly one audit event")
        extraction = result.extraction if isinstance(result, ExtractionSuccess) else None
        scores.append((sample.quality.tier, score_sample(sample, extraction)))
        if isinstance(result, ExtractionEscalation):
            escalations[result.reason] = escalations.get(result.reason, 0) + 1
        for call in result.calls:
            model_versions.add(call.model_version)
            prompt_versions.add(call.prompt_version)
            if call.usage is not None:
                input_tokens += call.usage.input_tokens
                output_tokens += call.usage.output_tokens
            if call.cost_usd is None:
                complete_cost = False
            else:
                cost += call.cost_usd
        samples.append(
            {
                "sample_id": sample.sample_id,
                "tier": sample.quality.tier,
                "status": result.status,
                "reason": result.reason if isinstance(result, ExtractionEscalation) else None,
                "model_calls": len(result.calls),
                "audit_event": audit.events[0].event_type,
            }
        )
        logger.info("baseline_sample_completed index=%d total=%d", index, len(manifest.samples))
    report: dict[str, object] = {
        "report_version": "extraction-baseline@v1",
        "scoring_version": SCORING_VERSION,
        "measured_at": datetime.now(UTC).isoformat(),
        "dataset": {
            "repository": "Voxel51/high-quality-invoice-images-for-ocr",
            "revision": manifest.revision,
            "manifest_sha256": manifest_hash,
            "metadata_sha256": manifest.metadata_sha256,
            "pipeline_version": manifest.pipeline_version,
            "sample_count": len(manifest.samples),
        },
        "model_versions": sorted(model_versions),
        "prompt_versions": sorted(prompt_versions),
        "tier_metrics": summarize(scores),
        "escalations": dict(sorted(escalations.items())),
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": format(cost, "f") if complete_cost else None,
        },
        "samples": samples,
    }
    encoded = json.dumps(report, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    await asyncio.to_thread(write_new_artifact, output, encoded)
    return report


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--reference-report", type=Path, default=REFERENCE_REPORT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    configure_logging()
    try:
        settings = BaselineSettings().gateway_settings()
        async with GatewayClient(settings) as gateway:
            await run_baseline(
                dataset=args.dataset,
                reference_report=args.reference_report,
                output=args.output,
                gateway=gateway,
            )
    except (OSError, ValueError, KeyError, ValidationError) as error:
        logger.error("baseline_failed error_type=%s", type(error).__name__)
        return 1
    logger.info("baseline_completed report=%s", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))

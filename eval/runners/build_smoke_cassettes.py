"""Record synthetic offline responses for one Compose workflow smoke case."""

import asyncio
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

import httpx2

from eval.baseline.run import BaselineAuditSink, PreparedReader
from eval.golden.schema import GoldenManifest, InvoiceLabel
from eval.runners.run_pipeline import COMMITTED_MANIFEST, RECORDED_DOCUMENT, RECORDED_SAMPLE_ID
from invoiceops_agent.agents.extraction import ExtractionAgent
from invoiceops_agent.agents.triage import TriageAgent
from invoiceops_agent.artifacts import write_new_artifact
from invoiceops_agent.gateway_client import (
    AliasPolicy,
    EmbeddingRequest,
    GatewayClient,
    GatewaySettings,
)
from invoiceops_agent.gateway_client.cassettes import CassetteTransport
from invoiceops_agent.schemas.common import model_digest
from invoiceops_agent.schemas.extraction import (
    ExtractionRequest,
    ExtractionSuccess,
    InvoiceExtraction,
)
from invoiceops_agent.schemas.similarity import EMBEDDING_DIMENSIONS, SUMMARY_VERSION
from invoiceops_agent.schemas.triage import TriageDraft, TriageEvidence, TriageFact, TriageRequest
from invoiceops_agent.tools.document_preflight import DocumentPreflight
from invoiceops_agent.tools.document_settings import DocumentSettings
from invoiceops_agent.tools.ingestion_schemas import RawDocument
from invoiceops_agent.tools.similarity import invoice_summary

OUTPUT = Path("eval/cassettes/smoke")


def _field(value: object) -> dict[str, object]:
    return {"value": value, "confidence": "0" if value is None else "0.99"}


def _extraction(label: InvoiceLabel) -> InvoiceExtraction:
    return InvoiceExtraction.model_validate(
        {
            "vendor_name": _field(label.vendor_name),
            "vendor_tax_id": _field(label.vendor_tax_id),
            "bank_account_iban": _field(label.bank_account_iban),
            "invoice_number": _field(label.invoice_number),
            "po_number": _field(label.po_number),
            "currency": _field(label.currency),
            "invoice_date": _field(label.invoice_date),
            "due_date": _field(label.due_date),
            "subtotal": _field(label.subtotal),
            "tax_amount": _field(label.tax_amount),
            "total_amount": _field(label.total_amount),
            "line_items": [
                {
                    "description": _field(line.description),
                    "quantity": _field(line.quantity),
                    "unit_price": _field(line.unit_price),
                    "tax_rate": _field(line.tax_rate),
                    "line_total": _field(line.line_total),
                }
                for line in label.line_items
            ],
        }
    )


def _fixture_response(request: httpx2.Request, extraction: InvoiceExtraction) -> httpx2.Response:
    body = json.loads(request.content)
    if request.url.path.endswith("/chat/completions"):
        content = (
            TriageDraft(
                recommended_action="APPROVE",
                summary="Synthetic invoice has no classified business exception.",
                rationale="The confidence gate requires a human review of this clean case.",
                evidence_refs=("taxonomy:status", "policy:status"),
            ).model_dump_json()
            if body["model"] == "triage-reasoner"
            else extraction.model_dump_json()
        )
        response = {
            "id": "chatcmpl-golden-smoke",
            "object": "chat.completion",
            "created": 1790164800,
            "model": body["model"],
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ],
            "usage": {"prompt_tokens": 200, "completion_tokens": 200, "total_tokens": 400},
        }
    elif request.url.path.endswith("/embeddings"):
        response = {
            "object": "list",
            "model": body["model"],
            "data": [{"object": "embedding", "index": 0, "embedding": [1, *([0] * 383)]}],
            "usage": {"prompt_tokens": 20, "total_tokens": 20},
        }
    else:
        raise ValueError("Unexpected smoke cassette request")
    return httpx2.Response(
        200,
        headers={"content-type": "application/json", "x-litellm-response-cost": "0.001"},
        json=response,
    )


async def record(output: Path) -> None:
    manifest = GoldenManifest.model_validate_json(COMMITTED_MANIFEST.read_bytes())
    sample = next(row for row in manifest.samples if row.sample_id == RECORDED_SAMPLE_ID)
    body = RECORDED_DOCUMENT.read_bytes()
    if hashlib.sha256(body).hexdigest() != sample.document_sha256:
        raise ValueError("Smoke document differs from the committed checksum")
    expected = _extraction(sample.label)
    transport = CassetteTransport(
        output,
        mode="record",
        upstream=httpx2.MockTransport(lambda request: _fixture_response(request, expected)),
    )
    settings = GatewaySettings(
        base_url="http://cassette.invalid/v1",
        api_key="synthetic-cassette-key",
        aliases={
            "extract-vision": AliasPolicy(
                model_version="extract-vision",
                model_name="extract-vision",
                allow_images=True,
                response_format="json_object",
            ),
            "embed": AliasPolicy(model_version="embed", model_name="embed"),
            "triage-reasoner": AliasPolicy(
                model_version="triage-reasoner",
                model_name="triage-reasoner",
                response_format="json_object",
            ),
        },
    )
    document = RawDocument(
        content_type="image/png",
        content_hash=sample.document_sha256,
        request_hash="0" * 64,
        body=body,
    )
    request = ExtractionRequest(
        run_id=UUID(int=1),
        invoice_id=UUID(int=2),
        trace_id="0" * 32,
        raw_ref=f"s3://golden/{document.object_key}",
        content_hash=document.content_hash,
        content_type=document.content_type,
    )
    async with GatewayClient(settings, transport=transport) as gateway:
        result = await ExtractionAgent(
            PreparedReader(document),
            DocumentPreflight(DocumentSettings()),
            gateway,
            BaselineAuditSink(),
        ).extract(request)
        if not isinstance(result, ExtractionSuccess):
            raise ValueError("Synthetic smoke extraction did not complete")
        await gateway.embed(
            EmbeddingRequest(
                run_id=request.run_id,
                trace_id=request.trace_id,
                prompt_version=SUMMARY_VERSION,
                scenario="near_duplicate",
                inputs=(invoice_summary(result.extraction),),
                dimensions=EMBEDDING_DIMENSIONS,
            )
        )
        evidence = TriageEvidence(
            facts=(
                TriageFact(ref="workflow:review", detail="Human review is required"),
                TriageFact(ref="extraction:status", detail="COMPLETED"),
                TriageFact(ref="taxonomy:status", detail="CLEAN"),
                TriageFact(ref="policy:status", detail="AUTO_APPROVE_ELIGIBLE"),
                TriageFact(ref="match:status", detail="PASS"),
                TriageFact(ref="validation:status", detail="PASS"),
            )
        )
        triage = await TriageAgent(gateway).prepare(
            TriageRequest(
                run_id=request.run_id,
                invoice_id=request.invoice_id,
                trace_id=request.trace_id,
                evidence=evidence,
                evidence_sha256=model_digest(evidence),
            )
        )
        if triage.status != "DRAFT":
            raise ValueError("Synthetic triage cassette did not produce a cited draft")


def main() -> None:
    with TemporaryDirectory() as directory:
        generated = Path(directory)
        asyncio.run(record(generated))
        paths = list(generated.glob("*.json"))
        if len(paths) != 3:
            raise ValueError("Smoke recording must contain extraction, embedding, and triage")
        OUTPUT.mkdir(parents=True, exist_ok=True)
        for path in paths:
            target = OUTPUT / path.name
            content = path.read_bytes()
            try:
                write_new_artifact(target, content)
            except FileExistsError:
                if target.read_bytes() != content:
                    raise ValueError("Committed smoke cassette differs") from None


if __name__ == "__main__":
    main()

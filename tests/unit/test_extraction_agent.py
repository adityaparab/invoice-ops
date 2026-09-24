"""Synthetic document-to-SDK extraction outcomes, repair boundaries, and committed audit seams."""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx2
import pytest
from tests.unit.extraction_support import (
    extraction_request,
    invoice_extraction,
    pdf_bytes,
    raw_document,
)

from invoiceops_agent.agents.extraction import ExtractionAgent, IdentifierOCR
from invoiceops_agent.agents.extraction_errors import ExtractionAuditFailed
from invoiceops_agent.agents.extraction_prompts import PROMPT_VERSION, REPAIR_PROMPT_VERSION
from invoiceops_agent.gateway_client import GatewayClient, GatewaySettings
from invoiceops_agent.gateway_client.cassettes import CassetteTransport
from invoiceops_agent.ledger.errors import LedgerStorageError
from invoiceops_agent.ledger.schemas import AppendEvent, LedgerEvent, VersionPins
from invoiceops_agent.schemas.documents import DocumentReference
from invoiceops_agent.schemas.extraction import ExtractionEscalation, ExtractionSuccess
from invoiceops_agent.schemas.ocr import OCRIdentifier, OCRIdentifiers
from invoiceops_agent.tools.document_errors import DocumentUnavailable
from invoiceops_agent.tools.document_preflight import DocumentPreflight
from invoiceops_agent.tools.document_settings import DocumentSettings
from invoiceops_agent.tools.ingestion_schemas import RawDocument

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
CASSETTES = Path(__file__).parents[1] / "cassettes" / "extraction" / "v4"


@dataclass
class MemoryReader:
    document: RawDocument = field(default_factory=raw_document)
    failure: bool = False
    calls: int = 0

    async def read(
        self, reference: DocumentReference, *, run_id: UUID, trace_id: str
    ) -> RawDocument:
        self.calls += 1
        if self.failure:
            raise DocumentUnavailable(run_id=run_id, trace_id=trace_id)
        return self.document


@dataclass
class CaptureAudit:
    commands: list[AppendEvent] = field(default_factory=list)
    failure: bool = False
    entered: asyncio.Event | None = None
    release: asyncio.Event | None = None

    async def append(self, command: AppendEvent, *, trace_id: str) -> LedgerEvent:
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.failure:
            raise LedgerStorageError(
                "Synthetic audit failure", run_id=command.run_id, trace_id=trace_id
            )
        self.commands.append(command)
        assert command.versions is not None
        assert command.versions.model_version is not None
        assert command.versions.prompt_version is not None
        return LedgerEvent(
            **command.model_dump(exclude={"versions"}),
            id=UUID(int=100 + len(self.commands)),
            sequence=len(self.commands),
            created_at=datetime(2026, 9, 23, tzinfo=UTC),
            versions=VersionPins(
                graph_version="graph@v1",
                model_version=command.versions.model_version,
                prompt_version=command.versions.prompt_version,
                policy_version="not-applicable@v1",
            ),
        )


def gateway_settings(**changes: object) -> GatewaySettings:
    return GatewaySettings.model_validate(
        {
            "base_url": "http://synthetic-gateway.test/v1",
            "api_key": "synthetic-key",
            "aliases": {
                "extract-vision": {
                    "model_version": "synthetic-extraction@v1",
                    "allow_images": True,
                    "allow_pdf": True,
                }
            },
            "max_attempts": 2,
            "backoff_seconds": 0,
            **changes,
        }
    )


def model_response(content: str | None = None, *, refusal: bool = False) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "chatcmpl-synthetic-extraction",
            "object": "chat.completion",
            "created": 1790164800,
            "model": "synthetic-extraction-model-v1",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": content
                        if content is not None
                        else invoice_extraction().model_dump_json(),
                        "refusal": "synthetic-refusal" if refusal else None,
                    },
                }
            ],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200},
        },
    )


def agent(
    client: GatewayClient,
    audit: CaptureAudit,
    reader: MemoryReader | None = None,
    identifier_ocr: IdentifierOCR | None = None,
) -> ExtractionAgent:
    return ExtractionAgent(
        reader or MemoryReader(),
        DocumentPreflight(DocumentSettings()),
        client,
        audit,
        identifier_ocr,
    )


async def test_success_preserves_business_mismatches_and_audits_provenance() -> None:
    calls: list[httpx2.Request] = []
    data = invoice_extraction().model_dump(mode="json")
    data["total_amount"]["value"] = "-999"

    def handle(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return model_response(json.dumps(data))

    audit = CaptureAudit()
    async with GatewayClient(gateway_settings(), transport=httpx2.MockTransport(handle)) as client:
        result = await agent(client, audit).extract(extraction_request())
    assert isinstance(result, ExtractionSuccess)
    assert result.extraction.total_amount.value == -999
    assert len(calls) == len(audit.commands) == 1
    assert json.loads(calls[0].content)["model"] == "extract-vision"
    command = audit.commands[0]
    assert command.event_type == "extraction.completed" and command.actor_type == "AGENT"
    assert command.versions is not None and command.versions.prompt_version == PROMPT_VERSION
    assert command.versions.model_version == "synthetic-extraction@v1"
    assert result.calls[0].usage is not None
    assert result.calls[0].usage.total_tokens == 1200
    assert result.calls[0].latency_ms is not None
    assert command.payload["content_hash"] == extraction_request().content_hash
    assert command.payload["preflight_version"] == "document-preflight@v1"


async def test_success_audits_ocr_correction_before_downstream_decisions() -> None:
    data = invoice_extraction().model_dump(mode="json")
    data["bank_account_iban"] = {"value": "GB00SYNTH0020260827002", "confidence": "1"}
    data["po_number"] = {"value": "WRONG-PO", "confidence": "1"}

    class FakeOCR:
        async def read(
            self, document: RawDocument, *, run_id: UUID, trace_id: str
        ) -> OCRIdentifiers:
            assert document.content_hash == extraction_request().content_hash
            return OCRIdentifiers(
                status="COMPLETE",
                bank_account_iban=OCRIdentifier(value="GB00SYNTH00202608270002", confidence=76),
                po_number=OCRIdentifier(value="SYN-PO-1", confidence=90),
            )

    audit = CaptureAudit()
    async with GatewayClient(
        gateway_settings(),
        transport=httpx2.MockTransport(lambda _: model_response(json.dumps(data))),
    ) as client:
        result = await agent(client, audit, identifier_ocr=FakeOCR()).extract(extraction_request())
    assert isinstance(result, ExtractionSuccess)
    assert result.extraction.bank_account_iban.value == "GB00SYNTH00202608270002"
    assert result.extraction.po_number.value == "SYN-PO-1"
    assert audit.commands[0].payload["identifier_ocr_applied_fields"] == [
        "bank_account_iban",
        "po_number",
    ]
    assert audit.commands[0].versions is not None
    assert audit.commands[0].versions.policy_version == "identifier-ocr@v1"


async def test_malformed_output_gets_one_fixed_repair_without_echoing_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bodies: list[str] = []
    caplog.set_level(logging.DEBUG)

    def handle(request: httpx2.Request) -> httpx2.Response:
        bodies.append(request.content.decode())
        response = (
            model_response('{"secret":"synthetic-private-model-output"}')
            if len(bodies) == 1
            else model_response()
        )
        return httpx2.Response(
            200, headers={"x-litellm-response-cost": "0.002"}, json=response.json()
        )

    audit = CaptureAudit()
    reader = MemoryReader()
    async with GatewayClient(gateway_settings(), transport=httpx2.MockTransport(handle)) as client:
        result = await agent(client, audit, reader).extract(extraction_request())
    assert isinstance(result, ExtractionSuccess)
    assert [call.status for call in result.calls] == ["MALFORMED", "VALID"]
    assert [call.cost_usd for call in result.calls] == [Decimal("0.002"), Decimal("0.002")]
    assert result.calls[-1].prompt_version == REPAIR_PROMPT_VERSION
    assert reader.calls == 1 and len(bodies) == 2
    assert "technical formatting" in bodies[-1]
    assert "synthetic-private-model-output" not in bodies[-1]
    assert "synthetic-private-model-output" not in caplog.text
    assert "synthetic-key" not in caplog.text


@pytest.mark.parametrize(
    "failure,expected,http_calls",
    [
        ("malformed", "MALFORMED_MODEL_OUTPUT", 2),
        ("refusal", "INVALID_MODEL_RESPONSE", 1),
        ("unauthorized", "GATEWAY_REJECTED", 1),
        ("unavailable", "GATEWAY_UNAVAILABLE", 2),
    ],
)
async def test_failure_paths_escalate_with_exact_repair_and_infra_attempt_limits(
    failure: str,
    expected: str,
    http_calls: int,
) -> None:
    calls = 0

    def handle(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if failure == "malformed":
            return model_response("invalid-json")
        if failure == "refusal":
            return model_response(refusal=True)
        return httpx2.Response(
            401 if failure == "unauthorized" else 503, json={"error": {"code": "synthetic-error"}}
        )

    audit = CaptureAudit()
    async with GatewayClient(gateway_settings(), transport=httpx2.MockTransport(handle)) as client:
        result = await agent(client, audit).extract(extraction_request())
    assert isinstance(result, ExtractionEscalation) and result.reason == expected
    assert calls == http_calls
    assert len(result.calls) == (2 if failure == "malformed" else 1)
    assert len(audit.commands) == 1 and audit.commands[0].event_type == "extraction.escalated"


@pytest.mark.parametrize("failure", ["storage", "unsupported"])
async def test_document_failures_are_audited_without_model_calls(failure: str) -> None:
    def forbidden(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("No gateway request allowed")

    settings = gateway_settings()
    if failure == "unsupported":
        settings = gateway_settings(aliases={"extract-vision": {"model_version": "synthetic@v1"}})
    audit = CaptureAudit()
    reader = MemoryReader(failure=failure == "storage")
    async with GatewayClient(settings, transport=httpx2.MockTransport(forbidden)) as client:
        result = await agent(client, audit, reader).extract(extraction_request())
    assert isinstance(result, ExtractionEscalation)
    assert result.reason == (
        "DOCUMENT_UNAVAILABLE" if failure == "storage" else "UNSUPPORTED_DOCUMENT"
    )
    assert not result.calls and len(audit.commands) == 1


async def test_success_is_not_returned_before_audit_commit_and_failures_propagate() -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    audit = CaptureAudit(entered=entered, release=release)
    async with GatewayClient(
        gateway_settings(), transport=httpx2.MockTransport(lambda _: model_response())
    ) as client:
        pending = asyncio.create_task(agent(client, audit).extract(extraction_request()))
        await entered.wait()
        assert not pending.done()
        release.set()
        assert isinstance(await pending, ExtractionSuccess)
        with pytest.raises(ExtractionAuditFailed):
            await agent(client, CaptureAudit(failure=True)).extract(extraction_request())


async def test_cancellation_propagates_without_audit_or_repair() -> None:
    entered = asyncio.Event()
    calls = 0

    async def handle(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        entered.set()
        await asyncio.Event().wait()
        return model_response()

    audit = CaptureAudit()
    async with GatewayClient(gateway_settings(), transport=httpx2.MockTransport(handle)) as client:
        pending = asyncio.create_task(agent(client, audit).extract(extraction_request()))
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert calls == 1 and not audit.commands


@pytest.mark.parametrize(
    "scenario,status,calls",
    [
        ("extract_success", "EXTRACTED", 1),
        ("extract_repaired", "EXTRACTED", 2),
        ("extract_malformed", "ESCALATED", 2),
        ("extract_refused", "ESCALATED", 1),
    ],
)
async def test_committed_offline_cassettes_cover_real_sdk_agent_boundary(
    scenario: str,
    status: str,
    calls: int,
) -> None:
    audit = CaptureAudit()
    async with GatewayClient(gateway_settings(), transport=CassetteTransport(CASSETTES)) as client:
        result = await agent(client, audit).extract(extraction_request(scenario=scenario))
    assert result.status == status and len(result.calls) == calls
    assert len(audit.commands) == 1


@pytest.mark.parametrize("allow_pdf", [False, True])
async def test_native_pdf_requires_explicit_alias_capability(allow_pdf: bool) -> None:
    document = raw_document(pdf_bytes(), "application/pdf")
    bodies: list[dict[str, object]] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        assert b'"filename":"invoice.pdf"' in request.content
        assert b"data:application/pdf;base64," in request.content
        return model_response()

    settings = gateway_settings(
        aliases={
            "extract-vision": {
                "model_version": "synthetic@v1",
                "allow_images": True,
                "allow_pdf": allow_pdf,
            }
        }
    )
    audit = CaptureAudit()
    reader = MemoryReader(document)
    async with GatewayClient(settings, transport=httpx2.MockTransport(handle)) as client:
        result = await agent(client, audit, reader).extract(extraction_request(document))
    if allow_pdf:
        assert isinstance(result, ExtractionSuccess) and len(bodies) == reader.calls == 1
    else:
        assert isinstance(result, ExtractionEscalation) and result.reason == "UNSUPPORTED_DOCUMENT"
        assert not bodies and reader.calls == 0
    assert len(audit.commands) == 1

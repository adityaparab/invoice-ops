"""Extract observations with one technical repair and commit the resulting audit event."""

import asyncio
import base64
import logging
from time import perf_counter
from typing import Protocol

from pydantic import BaseModel, ValidationError

from invoiceops_agent.agents.extraction_errors import (
    ExtractionAuditFailed,
    ExtractionConfigurationError,
)
from invoiceops_agent.agents.extraction_prompts import (
    PROMPT_VERSION,
    REPAIR_PROMPT_VERSION,
    ExtractionPrompts,
    load_prompts,
)
from invoiceops_agent.gateway_client import (
    AliasPolicy,
    FilePart,
    GatewayCassetteMismatch,
    GatewayConfigurationError,
    GatewayError,
    GatewayMessage,
    GatewayRequest,
    GatewayRequestRejected,
    GatewayResult,
    GatewayUnavailable,
    GuardrailRejected,
    ImagePart,
    InvalidStructuredOutput,
    TextPart,
    TokenBudgetExceeded,
)
from invoiceops_agent.gateway_client.schemas import ModelAlias, RequestContext
from invoiceops_agent.ledger.audit import AuditSink
from invoiceops_agent.ledger.errors import LedgerError
from invoiceops_agent.ledger.schemas import AppendEvent, VersionOverrides
from invoiceops_agent.obs.tracing import operation_span, traced_call
from invoiceops_agent.schemas.extraction import (
    EscalationReason,
    ExtractionEscalation,
    ExtractionRequest,
    ExtractionResult,
    ExtractionSuccess,
    InvoiceExtraction,
    ModelInvocation,
    ModelUsage,
)
from invoiceops_agent.tools.document_errors import (
    DocumentError,
    DocumentUnavailable,
    UnsupportedDocumentFeature,
)
from invoiceops_agent.tools.document_preflight import PREFLIGHT_VERSION, DocumentPreflight
from invoiceops_agent.tools.ingestion_schemas import RawDocument
from invoiceops_agent.tools.raw_document_reader import DocumentReader

logger = logging.getLogger(__name__)


class ExtractionGateway(Protocol):
    def configured_policy(self, alias: ModelAlias, context: RequestContext) -> AliasPolicy: ...

    async def complete[T: BaseModel](
        self, request: GatewayRequest, response_model: type[T]
    ) -> GatewayResult[T]: ...


def _context(request: ExtractionRequest, *, repair: bool = False) -> RequestContext:
    return RequestContext(
        run_id=request.run_id,
        trace_id=request.trace_id,
        prompt_version=REPAIR_PROMPT_VERSION if repair else PROMPT_VERSION,
        scenario=f"{request.scenario}_schema_repair" if repair else request.scenario,
    )


def _model_request(
    request: ExtractionRequest, document: RawDocument, prompts: ExtractionPrompts, *, repair: bool
) -> GatewayRequest:
    data_url = (
        f"data:{document.content_type};base64,{base64.b64encode(document.body).decode('ascii')}"
    )
    part = (
        FilePart(data_url=data_url)
        if document.content_type == "application/pdf"
        else ImagePart(data_url=data_url)
    )
    messages = [GatewayMessage(role="system", content=(TextPart(text=prompts.base),))]
    if repair:
        messages.append(GatewayMessage(role="system", content=(TextPart(text=prompts.repair),)))
    messages.append(GatewayMessage(role="user", content=(part,)))
    return GatewayRequest(
        **_context(request, repair=repair).model_dump(),
        alias="extract-vision",
        messages=tuple(messages),
    )


def _gateway_reason(error: GatewayError) -> EscalationReason:
    if isinstance(error, GatewayUnavailable):
        return "GATEWAY_UNAVAILABLE"
    if isinstance(error, GatewayRequestRejected):
        return "GATEWAY_REJECTED"
    if isinstance(error, GuardrailRejected):
        return "GUARDRAIL_REJECTED"
    if isinstance(error, TokenBudgetExceeded):
        return "TOKEN_BUDGET_EXCEEDED"
    return "INVALID_MODEL_RESPONSE"


def _escalation(
    request: ExtractionRequest, calls: list[ModelInvocation], reason: EscalationReason
) -> ExtractionEscalation:
    return ExtractionEscalation(
        run_id=request.run_id,
        invoice_id=request.invoice_id,
        trace_id=request.trace_id,
        calls=tuple(calls),
        reason=reason,
    )


class ExtractionAgent:
    def __init__(
        self,
        reader: DocumentReader,
        preflight: DocumentPreflight,
        gateway: ExtractionGateway,
        audit: AuditSink,
    ) -> None:
        self._reader = reader
        self._preflight = preflight
        self._gateway = gateway
        self._audit = audit

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        started = perf_counter()
        try:
            policy = self._gateway.configured_policy("extract-vision", _context(request))
            versions = VersionOverrides(
                model_version=policy.model_version, prompt_version=PROMPT_VERSION
            )
            prompts = await load_prompts()
        except (GatewayConfigurationError, ValidationError, OSError):
            raise ExtractionConfigurationError(request) from None
        try:
            result = await self._extract(request, policy, prompts)
            if result.calls:
                versions = VersionOverrides(
                    model_version=result.calls[-1].model_version,
                    prompt_version=result.calls[-1].prompt_version,
                )
            command = AppendEvent(
                run_id=request.run_id,
                invoice_id=request.invoice_id,
                event_type="extraction.completed"
                if isinstance(result, ExtractionSuccess)
                else "extraction.escalated",
                actor_type="AGENT",
                actor_id="invoiceops-extraction",
                node="Extract",
                versions=versions,
                payload={
                    "content_hash": request.content_hash,
                    "preflight_version": PREFLIGHT_VERSION,
                    "result": result.model_dump(mode="json"),
                },
            )
            try:
                await self._audit.append(command, trace_id=request.trace_id)
            except (LedgerError, TimeoutError, OSError):
                logger.error(
                    "extraction_audit_failed run_id=%s trace_id=%s",
                    request.run_id,
                    request.trace_id,
                )
                raise ExtractionAuditFailed(request) from None
        except asyncio.CancelledError:
            logger.info(
                "extraction_cancelled run_id=%s trace_id=%s", request.run_id, request.trace_id
            )
            raise
        logger.info(
            "extraction_completed run_id=%s invoice_id=%s trace_id=%s "
            "status=%s calls=%d duration_ms=%.3f",
            request.run_id,
            request.invoice_id,
            request.trace_id,
            result.status,
            len(result.calls),
            (perf_counter() - started) * 1000,
        )
        return result

    async def _extract(
        self, request: ExtractionRequest, policy: AliasPolicy, prompts: ExtractionPrompts
    ) -> ExtractionResult:
        calls: list[ModelInvocation] = []
        try:
            if (
                request.content_type == "application/pdf"
                and not policy.allow_pdf
                or request.content_type != "application/pdf"
                and not policy.allow_images
            ):
                raise UnsupportedDocumentFeature(run_id=request.run_id, trace_id=request.trace_id)
            document = await traced_call(
                "tool",
                "document_read",
                request,
                lambda: self._reader.read(
                    request, run_id=request.run_id, trace_id=request.trace_id
                ),
            )
            if (
                document.content_hash != request.content_hash
                or document.content_type != request.content_type
            ):
                raise DocumentError(run_id=request.run_id, trace_id=request.trace_id)
            await traced_call(
                "tool",
                "document_preflight",
                request,
                lambda: self._preflight.check(
                    document, run_id=request.run_id, trace_id=request.trace_id
                ),
            )
        except DocumentUnavailable:
            return _escalation(request, calls, "DOCUMENT_UNAVAILABLE")
        except UnsupportedDocumentFeature:
            return _escalation(request, calls, "UNSUPPORTED_DOCUMENT")
        except DocumentError:
            return _escalation(request, calls, "INVALID_DOCUMENT")
        for repair in (False, True):
            model_request = _model_request(request, document, prompts, repair=repair)
            try:
                with operation_span("tool", "extraction_gateway", request):
                    result = await self._gateway.complete(model_request, InvoiceExtraction)
            except (GatewayCassetteMismatch, GatewayConfigurationError):
                raise
            except GatewayError as error:
                malformed = isinstance(error, InvalidStructuredOutput)
                calls.append(
                    ModelInvocation(
                        model_version=policy.model_version,
                        prompt_version=model_request.prompt_version,
                        status="MALFORMED" if malformed else "FAILED",
                        gateway_attempts=error.attempts,
                    )
                )
                if malformed and not repair:
                    continue
                reason: EscalationReason = (
                    "MALFORMED_MODEL_OUTPUT" if malformed else _gateway_reason(error)
                )
                return _escalation(request, calls, reason)
            calls.append(
                ModelInvocation(
                    model_version=result.provenance.model_version,
                    prompt_version=result.provenance.prompt_version,
                    provider_model=result.provenance.model,
                    status="VALID",
                    gateway_attempts=result.attempts,
                    usage=ModelUsage.model_validate(result.usage.model_dump()),
                    latency_ms=result.latency_ms,
                    cost_usd=result.cost_usd,
                )
            )
            return ExtractionSuccess(
                run_id=request.run_id,
                invoice_id=request.invoice_id,
                trace_id=request.trace_id,
                calls=tuple(calls),
                extraction=result.value,
            )
        raise AssertionError("Two bounded extraction passes must produce an outcome")

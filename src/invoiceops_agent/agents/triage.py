"""Draft a cited exception recommendation through the sole gateway doorway."""

import asyncio
import json
import logging
from importlib.resources import files
from time import perf_counter
from typing import Protocol

from invoiceops_agent.gateway_client import (
    AliasPolicy,
    GatewayCassetteMismatch,
    GatewayConfigurationError,
    GatewayError,
    GatewayMessage,
    GatewayRequest,
    GatewayResult,
    TextPart,
)
from invoiceops_agent.gateway_client.schemas import ModelAlias, RequestContext
from invoiceops_agent.schemas.triage import TriageDraft, TriageRequest, TriageResult

logger = logging.getLogger(__name__)
PROMPT_VERSION = "triage@v1"


class TriageGateway(Protocol):
    def configured_policy(self, alias: ModelAlias, context: RequestContext) -> AliasPolicy: ...

    async def complete(
        self, request: GatewayRequest, response_model: type[TriageDraft]
    ) -> GatewayResult[TriageDraft]: ...


async def _load_prompt() -> str:
    return await asyncio.to_thread(
        lambda: (
            files("invoiceops_agent.prompts").joinpath("triage_v1.md").read_text(encoding="utf-8")
        )
    )


class TriageAgent:
    def __init__(self, gateway: TriageGateway) -> None:
        self._gateway = gateway

    async def prepare(self, request: TriageRequest) -> TriageResult:
        started = perf_counter()
        context = RequestContext(
            run_id=request.run_id,
            trace_id=request.trace_id,
            prompt_version=PROMPT_VERSION,
            scenario="triage",
        )
        policy = self._gateway.configured_policy("triage-reasoner", context)
        prompt = await _load_prompt()
        evidence = request.evidence.model_dump(mode="json")
        model_request = GatewayRequest(
            **context.model_dump(),
            alias="triage-reasoner",
            messages=(
                GatewayMessage(role="system", content=(TextPart(text=prompt),)),
                GatewayMessage(
                    role="user",
                    content=(TextPart(text=json.dumps(evidence, sort_keys=True)),),
                ),
            ),
        )
        try:
            response = await self._gateway.complete(model_request, TriageDraft)
        except (GatewayConfigurationError, GatewayCassetteMismatch):
            raise
        except GatewayError as error:
            logger.warning(
                "triage_model_fallback run_id=%s trace_id=%s error_type=%s duration_ms=%.3f",
                request.run_id,
                request.trace_id,
                type(error).__name__,
                (perf_counter() - started) * 1000,
            )
            return self._fallback(request, policy.model_version, "GATEWAY_FAILURE", error.attempts)
        draft = response.value
        valid_refs = {fact.ref for fact in request.evidence.facts}
        if not set(draft.evidence_refs) <= valid_refs:
            return self._fallback(
                request,
                response.provenance.model_version,
                "INVALID_EVIDENCE_REFS",
                response.attempts,
            )
        if draft.recommended_action == "APPROVE" and any(
            fact.ref == "policy:status" and fact.detail == "BLOCK"
            for fact in request.evidence.facts
        ):
            return self._fallback(
                request,
                response.provenance.model_version,
                "POLICY_CONFLICT",
                response.attempts,
            )
        logger.info(
            "triage_draft_ready run_id=%s trace_id=%s model=%s refs=%d duration_ms=%.3f",
            request.run_id,
            request.trace_id,
            response.provenance.model_version,
            len(draft.evidence_refs),
            (perf_counter() - started) * 1000,
        )
        return TriageResult(
            status="DRAFT",
            draft=draft,
            fallback_reason=None,
            evidence_sha256=request.evidence_sha256,
            model_version=response.provenance.model_version,
            gateway_attempts=response.attempts,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=response.latency_ms,
            cost_usd=response.cost_usd,
        )

    @staticmethod
    def _fallback(
        request: TriageRequest,
        model_version: str,
        reason: str,
        attempts: int,
    ) -> TriageResult:
        return TriageResult.model_validate(
            {
                "status": "FALLBACK",
                "draft": None,
                "fallback_reason": reason,
                "evidence_sha256": request.evidence_sha256,
                "model_version": model_version,
                "gateway_attempts": attempts,
            }
        )

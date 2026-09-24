"""Score advisory triage drafts through the versioned eval-judge gateway alias."""

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
from invoiceops_agent.schemas.common import model_digest
from invoiceops_agent.schemas.eval_judge import (
    JUDGE_PROMPT_VERSION,
    TriageJudgeRequest,
    TriageJudgeResult,
    TriageJudgeRubric,
)

logger = logging.getLogger(__name__)


class JudgeGateway(Protocol):
    def configured_policy(self, alias: ModelAlias, context: RequestContext) -> AliasPolicy: ...

    async def complete(
        self, request: GatewayRequest, response_model: type[TriageJudgeRubric]
    ) -> GatewayResult[TriageJudgeRubric]: ...


class EvalJudgeAgent:
    def __init__(self, gateway: JudgeGateway) -> None:
        self._gateway = gateway

    async def judge(self, request: TriageJudgeRequest) -> TriageJudgeResult:
        started = perf_counter()
        context = RequestContext(
            run_id=request.run_id,
            trace_id=request.trace_id,
            prompt_version=JUDGE_PROMPT_VERSION,
            scenario="golden_triage_judge",
        )
        policy = self._gateway.configured_policy("eval-judge", context)
        prompt = await asyncio.to_thread(
            lambda: (
                files("invoiceops_agent.prompts")
                .joinpath("triage_judge_v1.md")
                .read_text(encoding="utf-8")
            )
        )
        gateway_request = GatewayRequest(
            **context.model_dump(),
            alias="eval-judge",
            messages=(
                GatewayMessage(role="system", content=(TextPart(text=prompt),)),
                GatewayMessage(
                    role="user",
                    content=(
                        TextPart(text=json.dumps(request.model_dump(mode="json"), sort_keys=True)),
                    ),
                ),
            ),
        )
        try:
            response = await self._gateway.complete(gateway_request, TriageJudgeRubric)
        except (GatewayConfigurationError, GatewayCassetteMismatch):
            raise
        except GatewayError as error:
            logger.warning(
                "eval_judge_unavailable run_id=%s sample_id=%s error_type=%s duration_ms=%.3f",
                request.run_id,
                request.sample_id,
                type(error).__name__,
                (perf_counter() - started) * 1000,
            )
            return TriageJudgeResult(
                sample_id=request.sample_id,
                status="UNAVAILABLE",
                rubric=None,
                error_type=type(error).__name__,
                evidence_sha256=model_digest(request),
                model_version=policy.model_version,
            )
        logger.info(
            "eval_judge_scored run_id=%s sample_id=%s model_version=%s score=%d duration_ms=%.3f",
            request.run_id,
            request.sample_id,
            response.provenance.model_version,
            response.value.total,
            (perf_counter() - started) * 1000,
        )
        return TriageJudgeResult(
            sample_id=request.sample_id,
            status="SCORED",
            rubric=response.value,
            error_type=None,
            evidence_sha256=model_digest(request),
            model_version=response.provenance.model_version,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=response.cost_usd,
        )

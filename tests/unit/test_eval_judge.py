"""Versioned triage rubric uses only the direct gateway and structured facts."""

from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from invoiceops_agent.agents.eval_judge import EvalJudgeAgent
from invoiceops_agent.agents.eval_judge_settings import LiteLLMJudgeSettings
from invoiceops_agent.gateway_client import (
    AliasPolicy,
    GatewayRequest,
    GatewayResult,
    GatewayUnavailable,
    TextPart,
)
from invoiceops_agent.gateway_client.schemas import (
    GatewayProvenance,
    ModelAlias,
    RequestContext,
    TokenUsage,
)
from invoiceops_agent.schemas.eval_judge import (
    TriageJudgeRequest,
    TriageJudgeRubric,
)
from invoiceops_agent.schemas.triage import TriageDraft, TriageEvidence, TriageFact

pytestmark = pytest.mark.unit


class FakeJudgeGateway:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.requests: list[GatewayRequest] = []

    def configured_policy(self, alias: ModelAlias, context: RequestContext) -> AliasPolicy:
        assert alias == "eval-judge" and context.prompt_version == "triage-judge@v1"
        return AliasPolicy(model_version="synthetic-judge@v1", model_name="synthetic-judge@v1")

    async def complete(
        self, request: GatewayRequest, response_model: type[TriageJudgeRubric]
    ) -> GatewayResult[TriageJudgeRubric]:
        self.requests.append(request)
        assert response_model is TriageJudgeRubric
        if self.fail:
            raise GatewayUnavailable(
                RequestContext(
                    run_id=request.run_id,
                    trace_id=request.trace_id,
                    prompt_version=request.prompt_version,
                    scenario=request.scenario,
                ),
                attempts=1,
            )
        return GatewayResult(
            value=TriageJudgeRubric(
                evidence_support=2,
                action_safety=2,
                clarity=1,
                rationale="The recommendation cites the exception and keeps review with a person.",
            ),
            provenance=GatewayProvenance(
                alias="eval-judge",
                model="synthetic-judge@v1",
                model_version="synthetic-judge@v1",
                prompt_version="triage-judge@v1",
            ),
            usage=TokenUsage(input_tokens=30, output_tokens=12, total_tokens=42),
            attempts=1,
            latency_ms=3,
            cost_usd=Decimal("0.002"),
        )


def request() -> TriageJudgeRequest:
    return TriageJudgeRequest(
        sample_id="SYN-ANOM-0001",
        run_id=UUID(int=1),
        trace_id="a" * 32,
        evidence=TriageEvidence(
            facts=(TriageFact(ref="taxonomy:PRICE_MM", detail="Unit price differs"),)
        ),
        draft=TriageDraft(
            recommended_action="ESCALATE",
            summary="Price variance requires review",
            rationale="The PO and invoice unit prices differ",
            evidence_refs=("taxonomy:PRICE_MM",),
        ),
        expected_codes=("PRICE_MM",),
        observed_codes=("PRICE_MM",),
    )


@pytest.mark.asyncio
async def test_judge_scores_versioned_rubric_and_pins_model_cost() -> None:
    gateway = FakeJudgeGateway()
    result = await EvalJudgeAgent(gateway).judge(request())
    assert result.status == "SCORED" and result.rubric is not None
    assert result.rubric.total == 5
    assert result.prompt_version == "triage-judge@v1"
    assert result.model_version == "synthetic-judge@v1"
    assert result.cost_usd == Decimal("0.002")
    assert gateway.requests[0].alias == "eval-judge"
    user_part = gateway.requests[0].messages[-1].content[0]
    assert isinstance(user_part, TextPart)
    assert "PRICE_MM" in user_part.text


@pytest.mark.asyncio
async def test_gateway_failure_keeps_judge_unavailable() -> None:
    result = await EvalJudgeAgent(FakeJudgeGateway(fail=True)).judge(request())
    assert result.status == "UNAVAILABLE"
    assert result.rubric is None
    assert result.error_type == "GatewayUnavailable"


def test_judge_settings_read_only_url_key_and_explicit_model_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LITELLM_API_BASE", "https://gateway.example.test/v1")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "synthetic-key")
    monkeypatch.setenv("LITELLM_JUDGE_MODEL", "synthetic-judge")
    settings = LiteLLMJudgeSettings(_env_file=None).gateway_settings()
    assert str(settings.base_url) == "https://gateway.example.test/v1"
    assert settings.api_key.get_secret_value() == "synthetic-key"
    assert set(settings.aliases) == {"eval-judge"}
    assert settings.aliases["eval-judge"].model_name == "synthetic-judge"
    monkeypatch.delenv("LITELLM_JUDGE_MODEL")
    with pytest.raises(ValidationError):
        LiteLLMJudgeSettings(_env_file=None)

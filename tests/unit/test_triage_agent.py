"""Offline cited triage drafts and fail-closed fallback behavior."""

from uuid import UUID

import pytest

from invoiceops_agent.agents.triage import TriageAgent
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
from invoiceops_agent.schemas.common import model_digest
from invoiceops_agent.schemas.exceptions import TaxonomyRequest
from invoiceops_agent.schemas.triage import (
    TriageDraft,
    TriageEvidence,
    TriageFact,
    TriageRequest,
)
from invoiceops_agent.tools.exception_taxonomy import classify_exceptions
from invoiceops_agent.tools.triage_evidence import gather_triage_evidence

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
RUN_ID = UUID(int=2)
INVOICE_ID = UUID(int=1)
TRACE_ID = "a" * 32


class FakeTriageGateway:
    def __init__(self, draft: TriageDraft | None = None, *, fail: bool = False) -> None:
        self.draft = draft or TriageDraft(
            recommended_action="ESCALATE",
            summary="Near duplicate needs a person",
            rationale="The similarity finding requires verification",
            evidence_refs=("taxonomy:DUP_NEAR",),
        )
        self.fail = fail
        self.requests: list[GatewayRequest] = []

    def configured_policy(self, alias: ModelAlias, context: RequestContext) -> AliasPolicy:
        assert alias == "triage-reasoner"
        return AliasPolicy(model_version="synthetic-triage@v1", model_name="synthetic-triage@v1")

    async def complete(
        self, request: GatewayRequest, response_model: type[TriageDraft]
    ) -> GatewayResult[TriageDraft]:
        self.requests.append(request)
        assert response_model is TriageDraft
        if self.fail:
            raise GatewayUnavailable(
                RequestContext(
                    run_id=request.run_id,
                    trace_id=request.trace_id,
                    prompt_version=request.prompt_version,
                    scenario=request.scenario,
                ),
                attempts=2,
            )
        return GatewayResult(
            value=self.draft,
            provenance=GatewayProvenance(
                alias="triage-reasoner",
                model="synthetic-triage@v1",
                model_version="synthetic-triage@v1",
                prompt_version="triage@v1",
            ),
            usage=TokenUsage(input_tokens=30, output_tokens=12, total_tokens=42),
            attempts=1,
            latency_ms=3,
        )


def _request(*, blocked: bool = False) -> TriageRequest:
    facts = [
        TriageFact(ref="workflow:review", detail="Human review is required"),
        TriageFact(ref="taxonomy:DUP_NEAR", detail="SIMILARITY.content_hash"),
    ]
    if blocked:
        facts.append(TriageFact(ref="policy:status", detail="BLOCK"))
    evidence = TriageEvidence(facts=tuple(facts))
    return TriageRequest(
        run_id=RUN_ID,
        invoice_id=INVOICE_ID,
        trace_id=TRACE_ID,
        evidence=evidence,
        evidence_sha256=model_digest(evidence),
    )


async def test_cited_draft_is_versioned_and_uses_only_structured_evidence() -> None:
    gateway = FakeTriageGateway()
    result = await TriageAgent(gateway).prepare(_request())
    assert result.status == "DRAFT"
    assert result.draft is not None and result.draft.recommended_action == "ESCALATE"
    assert result.model_version == "synthetic-triage@v1"
    assert result.prompt_version == "triage@v1"
    assert result.input_tokens == 30 and result.output_tokens == 12
    assert gateway.requests[0].alias == "triage-reasoner"
    user_part = gateway.requests[0].messages[-1].content[0]
    assert isinstance(user_part, TextPart)
    assert "SIMILARITY.content_hash" in user_part.text


@pytest.mark.parametrize(
    "draft,blocked,reason",
    [
        (
            TriageDraft(
                recommended_action="RETURN",
                summary="Unknown citation",
                rationale="Needs work",
                evidence_refs=("unknown:source",),
            ),
            False,
            "INVALID_EVIDENCE_REFS",
        ),
        (
            TriageDraft(
                recommended_action="APPROVE",
                summary="Unsafe approval",
                rationale="Ignore policy",
                evidence_refs=("policy:status",),
            ),
            True,
            "POLICY_CONFLICT",
        ),
    ],
)
async def test_uncited_or_policy_conflicting_draft_falls_back(
    draft: TriageDraft, blocked: bool, reason: str
) -> None:
    result = await TriageAgent(FakeTriageGateway(draft)).prepare(_request(blocked=blocked))
    assert result.status == "FALLBACK" and result.draft is None
    assert result.fallback_reason == reason


async def test_gateway_failure_falls_back_to_human_review() -> None:
    result = await TriageAgent(FakeTriageGateway(fail=True)).prepare(_request())
    assert result.status == "FALLBACK"
    assert result.fallback_reason == "GATEWAY_FAILURE"
    assert result.gateway_attempts == 2


async def test_evidence_gathering_uses_taxonomy_refs_without_raw_invoice_text() -> None:
    taxonomy = classify_exceptions(
        TaxonomyRequest(
            run_id=RUN_ID, invoice_id=INVOICE_ID, trace_id=TRACE_ID, near_duplicate=True
        )
    )
    evidence = gather_triage_evidence(
        taxonomy=taxonomy,
        policy=None,
        match=None,
        validation=None,
        extraction_escalated=False,
    )
    assert [fact.ref for fact in evidence.facts] == [
        "workflow:review",
        "extraction:status",
        "taxonomy:status",
        "taxonomy:DUP_NEAR",
    ]
    assert "DUP_NEAR" in evidence.model_dump_json()
    assert evidence == gather_triage_evidence(
        taxonomy=taxonomy,
        policy=None,
        match=None,
        validation=None,
        extraction_escalated=False,
    )

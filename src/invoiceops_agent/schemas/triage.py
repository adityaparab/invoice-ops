"""Versioned evidence and structured advisory triage contracts."""

from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from invoiceops_agent.schemas.common import model_digest


class TriageModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class TriageFact(TriageModel):
    ref: str = Field(pattern=r"^[a-z_]+:[A-Za-z0-9_:.\-]+$", max_length=100)
    detail: str = Field(min_length=1, max_length=500)


class TriageEvidence(TriageModel):
    version: Literal["triage-evidence@v1"] = "triage-evidence@v1"
    facts: tuple[TriageFact, ...] = Field(min_length=1, max_length=150)

    @model_validator(mode="after")
    def unique_refs(self) -> Self:
        if len({fact.ref for fact in self.facts}) != len(self.facts):
            raise ValueError("Triage evidence references must be unique")
        return self


class TriageRequest(TriageModel):
    run_id: UUID
    invoice_id: UUID
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    evidence: TriageEvidence
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def coherent_evidence(self) -> Self:
        if self.evidence_sha256 != model_digest(self.evidence):
            raise ValueError("Triage evidence fingerprint does not match")
        return self


class TriageDraft(TriageModel):
    recommended_action: Literal["APPROVE", "RETURN", "ESCALATE"]
    summary: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=2000)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_citations(self) -> Self:
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("Triage draft evidence references must be unique")
        return self


class TriageResult(TriageModel):
    status: Literal["DRAFT", "FALLBACK"]
    draft: TriageDraft | None
    fallback_reason: Literal["GATEWAY_FAILURE", "INVALID_EVIDENCE_REFS", "POLICY_CONFLICT"] | None
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_version: str = Field(min_length=1, max_length=160)
    prompt_version: Literal["triage@v1"] = "triage@v1"
    gateway_attempts: int = Field(ge=0, le=6)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    latency_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cost_usd: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def coherent_outcome(self) -> Self:
        if (self.status == "DRAFT") != (self.draft is not None):
            raise ValueError("Only successful triage results can contain a draft")
        if (self.status == "FALLBACK") != (self.fallback_reason is not None):
            raise ValueError("Fallback reason must match result status")
        return self

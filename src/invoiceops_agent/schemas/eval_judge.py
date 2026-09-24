"""Versioned advisory rubric for golden-set triage diagnostics."""

from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from invoiceops_agent.schemas.exceptions import ExceptionCode
from invoiceops_agent.schemas.triage import TriageDraft, TriageEvidence

JUDGE_PROMPT_VERSION: Literal["triage-judge@v1"] = "triage-judge@v1"


class JudgeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class TriageJudgeRequest(JudgeModel):
    sample_id: str = Field(min_length=1, max_length=100)
    run_id: UUID
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    evidence: TriageEvidence
    draft: TriageDraft
    expected_codes: tuple[ExceptionCode, ...]
    observed_codes: tuple[ExceptionCode, ...]


class TriageJudgeRubric(JudgeModel):
    evidence_support: int = Field(ge=0, le=2)
    action_safety: int = Field(ge=0, le=2)
    clarity: int = Field(ge=0, le=2)
    rationale: str = Field(min_length=1, max_length=1000)

    @property
    def total(self) -> int:
        return self.evidence_support + self.action_safety + self.clarity


class TriageJudgeResult(JudgeModel):
    sample_id: str
    status: Literal["SCORED", "UNAVAILABLE"]
    rubric: TriageJudgeRubric | None
    error_type: str | None
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_version: Literal["triage-judge@v1"] = JUDGE_PROMPT_VERSION
    model_version: str = Field(min_length=1, max_length=160)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def consistent_result(self) -> Self:
        if (self.status == "SCORED") != (self.rubric is not None):
            raise ValueError("Only scored results carry a rubric")
        if (self.status == "UNAVAILABLE") != (self.error_type is not None):
            raise ValueError("Unavailable results require an error type")
        return self

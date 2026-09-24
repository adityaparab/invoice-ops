"""Two-person exception decision transport contracts."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

DecisionAction = Literal["APPROVE", "RETURN", "ESCALATE"]
DecisionStage = Literal["PENDING_SIGNOFF", "QUEUED_FOR_RESUME"]


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: DecisionAction
    rationale: str = Field(min_length=1, max_length=2000)
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    proposal_id: UUID | None = None

    @field_validator("rationale")
    @classmethod
    def nonblank_rationale(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Decision rationale must contain non-whitespace text")
        return cleaned


class DecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: UUID
    exception_id: UUID
    run_id: UUID
    invoice_id: UUID
    action: DecisionAction
    actor_id: str
    stage: DecisionStage
    proposal_id: UUID | None

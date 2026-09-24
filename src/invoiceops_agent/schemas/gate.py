"""Legacy provisional and versioned composite confidence-gate contracts."""

from decimal import Decimal, localcontext
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from invoiceops_agent.schemas.common import ExactDecimal


class GateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    version: str = "provisional-gate@v1"
    auto_approval_enabled: bool = False
    minimum_field_confidence: ExactDecimal = Field(default=Decimal("0.99"), ge=0, le=1)


class GateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    route: Literal["AUTO_APPROVE", "REVIEW"]
    minimum_observed_confidence: ExactDecimal | None
    reason: Literal["POLICY", "LOW_CONFIDENCE", "AUTO_DISABLED", "ELIGIBLE"]
    extraction_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config: GateConfig


class CompositeGateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    version: Literal["composite-gate@v1", "composite-gate@v2"] = "composite-gate@v2"
    weight_field: ExactDecimal = Field(default=Decimal("0.50"), ge=0, le=1)
    weight_match: ExactDecimal = Field(default=Decimal("0.30"), ge=0, le=1)
    weight_policy: ExactDecimal = Field(default=Decimal("0.20"), ge=0, le=1)
    threshold: ExactDecimal = Field(default=Decimal("0.95"), ge=0, le=1)
    delta_denominator_floor: ExactDecimal = Field(default=Decimal("1"), gt=0)
    auto_approval_enabled: bool = True

    @model_validator(mode="after")
    def weights_sum_to_one(self) -> Self:
        with localcontext() as context:
            context.prec = 28
            if self.weight_field + self.weight_match + self.weight_policy != 1:
                raise ValueError("Composite gate weights must sum to one")
        return self

    def score(self, field_term: Decimal, match_delta: Decimal, policy_term: Decimal) -> Decimal:
        with localcontext() as context:
            context.prec = 28
            return (
                self.weight_field * field_term
                + self.weight_match * (1 - match_delta)
                + self.weight_policy * policy_term
            )


class CompositeGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    route: Literal["AUTO_APPROVE", "REVIEW"]
    reason: Literal["POLICY", "BELOW_THRESHOLD", "AUTO_DISABLED", "ELIGIBLE"]
    score: ExactDecimal = Field(ge=0, le=1)
    minimum_field_confidence: ExactDecimal = Field(ge=0, le=1)
    normalized_match_delta: ExactDecimal = Field(ge=0, le=1)
    policy_severity_term: ExactDecimal = Field(ge=0, le=1)
    policy_status: Literal["AUTO_APPROVE_ELIGIBLE", "REVIEW", "BLOCK"]
    extraction_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    match_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config: CompositeGateConfig

    @model_validator(mode="after")
    def coherent_route(self) -> Self:
        expected_term = {
            "AUTO_APPROVE_ELIGIBLE": Decimal(1),
            "REVIEW": Decimal("0.5"),
            "BLOCK": Decimal(0),
        }[self.policy_status]
        if self.policy_severity_term != expected_term:
            raise ValueError("Policy severity term does not match policy status")
        expected_score = self.config.score(
            self.minimum_field_confidence,
            self.normalized_match_delta,
            self.policy_severity_term,
        )
        if self.score != expected_score:
            raise ValueError("Composite score does not match its recorded terms")
        expected_reason = (
            "POLICY"
            if self.policy_status != "AUTO_APPROVE_ELIGIBLE"
            else "AUTO_DISABLED"
            if not self.config.auto_approval_enabled
            else "BELOW_THRESHOLD"
            if self.score < self.config.threshold
            else "ELIGIBLE"
        )
        if self.reason != expected_reason:
            raise ValueError("Gate reason does not match policy and threshold")
        if self.route != ("AUTO_APPROVE" if expected_reason == "ELIGIBLE" else "REVIEW"):
            raise ValueError("Gate route does not match its reason")
        return self


GateOutcome = GateResult | CompositeGateResult

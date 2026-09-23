"""Conservative interim gate until the versioned composite gate is implemented."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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

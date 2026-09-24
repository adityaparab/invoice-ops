"""Versioned deterministic exception queue projection contract."""

from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator


class QueueConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(default="exception-queue@v1", pattern=r"^[A-Za-z0-9@_.:-]{1,128}$")
    critical_sla_hours: int = Field(default=4, ge=1, le=168)
    high_sla_hours: int = Field(default=24, ge=1, le=720)
    normal_sla_hours: int = Field(default=72, ge=1, le=720)

    @model_validator(mode="after")
    def ordered_sla(self) -> Self:
        if not self.critical_sla_hours < self.high_sla_hours < self.normal_sla_hours:
            raise ValueError("Queue SLA durations must ascend by priority")
        return self


class QueueProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    exception_type: str = Field(min_length=1, max_length=128)
    priority: int = Field(ge=0, le=3)
    sla_due_at: AwareDatetime
    evidence: dict[str, JsonValue]
    recommendation: dict[str, JsonValue]
    config: QueueConfig

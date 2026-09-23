"""Liveness and dependency-readiness response contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

DependencyStatus = Literal["ok", "unavailable", "timeout", "unconfigured"]


class LivenessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"


class DependencyStatuses(BaseModel):
    model_config = ConfigDict(extra="forbid")

    postgres: DependencyStatus
    minio: DependencyStatus


class ReadinessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready"] = "ready"
    dependencies: DependencyStatuses

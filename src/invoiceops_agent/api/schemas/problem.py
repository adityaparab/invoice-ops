"""RFC 7807 problem details with correlation and optional readiness extensions."""

from pydantic import BaseModel, ConfigDict, Field

from invoiceops_agent.api.schemas.health import DependencyStatuses


class ProblemDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = "about:blank"
    title: str
    status: int = Field(ge=400, le=599)
    detail: str
    instance: str
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    dependencies: DependencyStatuses | None = None

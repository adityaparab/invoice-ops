"""Versioned state contract for the explicitly non-business hello scaffold."""

from typing import Literal, Self, TypedDict
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

HelloNodeName = Literal["hello_start", "hello_finish"]
HelloStatus = Literal["queued", "running", "completed"]


class GraphState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    run_id: UUID
    invoice_id: UUID
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    graph_version: Literal["hello-v1"] = "hello-v1"
    workflow: Literal["hello-stubs"] = "hello-stubs"
    status: HelloStatus = "queued"
    completed_nodes: list[HelloNodeName] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_progress(self) -> Self:
        expected: dict[HelloStatus, list[HelloNodeName]] = {
            "queued": [],
            "running": ["hello_start"],
            "completed": ["hello_start", "hello_finish"],
        }
        if self.completed_nodes != expected[self.status]:
            raise ValueError("Completed nodes must match the hello workflow status")
        return self


class StateUpdate(TypedDict):
    status: HelloStatus
    completed_nodes: list[HelloNodeName]

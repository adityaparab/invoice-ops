"""Versioned state contract for the explicitly non-business hello scaffold."""

from datetime import date
from typing import Literal, Self, TypedDict
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from invoiceops_agent.schemas.documents import DocumentType

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


InvoiceNodeName = Literal[
    "Ingest",
    "Extract",
    "Validate",
    "Match3Way",
    "Policy",
    "Gate",
    "AutoApprove",
    "ExceptionTriage",
    "HumanReview",
    "Archive",
    "Reject",
]
InvoiceStatus = Literal["queued", "running", "awaiting_review", "completed", "rejected"]
InvoiceRoute = Literal["AUTO_APPROVE", "REVIEW", "REJECT"]


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    action: Literal["APPROVE", "RETURN", "ESCALATE"]
    actor_id: str = Field(min_length=1, max_length=128)
    rationale: str = Field(min_length=1, max_length=2000, repr=False)
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")


class InvoiceGraphState(BaseModel):
    """Checkpoint-safe JSON evidence for the invoice-v1 workflow."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    run_id: UUID
    invoice_id: UUID
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    graph_version: Literal["invoice-v1"] = "invoice-v1"
    as_of: date
    raw_ref: str = Field(min_length=1, max_length=256, repr=False)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    content_type: DocumentType
    duplicate: bool = False
    status: InvoiceStatus = "queued"
    route: InvoiceRoute | None = None
    extraction_outcome: dict[str, JsonValue] | None = None
    extraction: dict[str, JsonValue] | None = None
    validation: dict[str, JsonValue] | None = None
    match: dict[str, JsonValue] | None = None
    snapshot: dict[str, JsonValue] | None = None
    similarity: dict[str, JsonValue] | None = None
    taxonomy: dict[str, JsonValue] | None = None
    policy: dict[str, JsonValue] | None = None
    gate: dict[str, JsonValue] | None = None
    triage: dict[str, JsonValue] | None = None
    review: dict[str, JsonValue] | None = None
    completed_nodes: list[InvoiceNodeName] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_progress(self) -> Self:
        if len(self.completed_nodes) != len(set(self.completed_nodes)):
            raise ValueError("Invoice workflow nodes cannot complete twice")
        if self.status == "completed" and "Archive" not in self.completed_nodes:
            raise ValueError("Completed invoice workflow requires Archive")
        if self.status == "rejected" and "Reject" not in self.completed_nodes:
            raise ValueError("Rejected invoice workflow requires Reject")
        return self

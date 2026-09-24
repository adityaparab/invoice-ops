"""A bounded, audited view of invoice workflow progress."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

from invoiceops_agent.api.schemas.invoice_read import RunStatus
from invoiceops_agent.graph.state import InvoiceNodeName


class RunProgressModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class NodeProgress(RunProgressModel):
    name: InvoiceNodeName
    observed_at: AwareDatetime | None = None
    event_type: str | None = None
    state: dict[str, JsonValue] = Field(default_factory=dict)


class RunProgress(RunProgressModel):
    run_id: UUID
    invoice_id: UUID
    status: RunStatus
    graph_version: str
    active_node: InvoiceNodeName | None
    progress_source: Literal["audit-ledger"] = "audit-ledger"
    nodes: list[NodeProgress] = Field(min_length=11, max_length=11)
    started_at: AwareDatetime | None
    completed_at: AwareDatetime | None
    read_at: AwareDatetime

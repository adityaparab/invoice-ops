"""Auditor-facing event trace and cross-run invoice provenance contracts."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from invoiceops_agent.api.schemas.invoice_read import InvoiceSource, InvoiceStatus, RunStatus
from invoiceops_agent.ledger.schemas import (
    ActorType,
    InvoiceCursor,
    LedgerEvent,
    RunCursor,
    TraceId,
    UtcDatetime,
    Version,
    VersionPins,
)


class ProvenanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class TraceEvent(ProvenanceModel):
    id: UUID
    sequence: int = Field(ge=1)
    event_type: Version
    node: Version | None
    actor_type: ActorType
    actor_id: Version
    supersedes_id: UUID | None
    versions: VersionPins
    created_at: UtcDatetime


class RunTracePage(ProvenanceModel):
    run_id: UUID
    invoice_id: UUID
    trace_id: TraceId
    status: RunStatus
    graph_version: Version
    started_at: UtcDatetime | None
    completed_at: UtcDatetime | None
    events: list[TraceEvent] = Field(max_length=200)
    next_cursor: RunCursor | None


class InvoiceProvenancePage(ProvenanceModel):
    invoice_id: UUID
    status: InvoiceStatus
    source: InvoiceSource
    created_at: UtcDatetime
    events: list[LedgerEvent] = Field(max_length=200)
    next_cursor: InvoiceCursor | None

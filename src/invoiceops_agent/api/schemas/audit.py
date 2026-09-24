"""Paginated, role-protected run audit history."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from invoiceops_agent.api.schemas.invoice_read import RunStatus
from invoiceops_agent.ledger.schemas import LedgerEvent, RunCursor, TraceId


class AuditRunPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    run_id: UUID
    invoice_id: UUID
    trace_id: TraceId
    status: RunStatus
    events: list[LedgerEvent] = Field(max_length=200)
    next_cursor: RunCursor | None

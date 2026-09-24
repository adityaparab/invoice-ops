"""Bounded invoice queue and aggregate detail response contracts."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

from invoiceops_agent.schemas.common import ExactDecimal
from invoiceops_agent.schemas.documents import DocumentType

InvoiceStatus = Literal[
    "RECEIVED",
    "QUEUED",
    "PROCESSING",
    "NEEDS_REVIEW",
    "APPROVED",
    "REJECTED",
    "RETURNED",
    "ARCHIVED",
    "FAILED",
]
RunStatus = Literal["QUEUED", "RUNNING", "PAUSED", "COMPLETED", "FAILED", "CANCELLED"]
ExceptionStatus = Literal["OPEN", "IN_REVIEW", "RESOLVED", "ESCALATED"]
InvoiceSource = Literal["UPLOAD", "EMAIL"]


class InvoiceReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class InvoiceListQuery(InvoiceReadModel):
    status: InvoiceStatus | None = None
    run_status: RunStatus | None = None
    source: InvoiceSource | None = None
    exception_only: bool = False
    min_priority: int | None = Field(default=None, ge=0, le=3)
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=200)


class InvoiceException(InvoiceReadModel):
    id: UUID
    exception_type: str = Field(min_length=1, max_length=128)
    status: ExceptionStatus
    priority: int = Field(ge=0, le=3)
    sla_due_at: AwareDatetime
    assigned_to: str | None = Field(default=None, max_length=128)
    evidence: dict[str, JsonValue]
    recommendation: dict[str, JsonValue]
    created_at: AwareDatetime


class InvoiceSummary(InvoiceReadModel):
    id: UUID
    run_id: UUID
    status: InvoiceStatus
    run_status: RunStatus
    source: InvoiceSource
    content_type: DocumentType
    vendor_name: str | None = Field(default=None, max_length=256)
    invoice_number: str | None = Field(default=None, max_length=128)
    po_number: str | None = Field(default=None, max_length=128)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    total_amount: ExactDecimal | None = None
    exception_id: UUID | None = None
    exception_type: str | None = Field(default=None, max_length=128)
    exception_priority: int | None = Field(default=None, ge=0, le=3)
    exception_sla_due_at: AwareDatetime | None = None
    created_at: AwareDatetime


class InvoicePage(InvoiceReadModel):
    items: list[InvoiceSummary] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=200)


class InvoiceDetail(InvoiceReadModel):
    invoice: InvoiceSummary
    exception: InvoiceException | None
    evidence: dict[str, dict[str, JsonValue]]
    read_at: AwareDatetime

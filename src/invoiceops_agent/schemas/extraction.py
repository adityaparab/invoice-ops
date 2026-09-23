"""Bounded observations from a document; arithmetic correctness is a separate decision."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    model_validator,
)

from invoiceops_agent.schemas.documents import DocumentReference


def decimal_text(value: Decimal) -> str:
    """Canonical decimal strings keep JSON consumers out of binary floating point."""
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


DecimalJson = PlainSerializer(decimal_text, return_type=str, when_used="json")

Confidence = Annotated[
    Decimal, Field(ge=0, le=1, max_digits=7, decimal_places=6, allow_inf_nan=False), DecimalJson
]
Amount = Annotated[
    Decimal, Field(max_digits=18, decimal_places=4, allow_inf_nan=False), DecimalJson
]
Quantity = Annotated[
    Decimal, Field(max_digits=18, decimal_places=6, allow_inf_nan=False), DecimalJson
]
TaxRate = Annotated[
    Decimal, Field(max_digits=12, decimal_places=6, allow_inf_nan=False), DecimalJson
]
VendorName = Annotated[str, StringConstraints(min_length=1, max_length=256)]
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Iban = Annotated[str, StringConstraints(min_length=1, max_length=64)]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
Description = Annotated[str, StringConstraints(min_length=1, max_length=1000)]


class ExtractionModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, revalidate_instances="always", hide_input_in_errors=True
    )


class ExtractedField[T](ExtractionModel):
    value: T | None = Field(repr=False)
    confidence: Confidence

    @model_validator(mode="after")
    def validate_unknown_confidence(self) -> Self:
        if self.value is None and self.confidence != 0:
            raise ValueError("Unknown values must have zero confidence")
        return self


class InvoiceLineItem(ExtractionModel):
    description: ExtractedField[Description]
    quantity: ExtractedField[Quantity]
    unit_price: ExtractedField[Amount]
    # A fraction: 0.20 represents 20 percent, not the number 20.
    tax_rate: ExtractedField[TaxRate]
    line_total: ExtractedField[Amount] = Field(description="Net line amount excluding tax")


class InvoiceExtraction(ExtractionModel):
    vendor_name: ExtractedField[VendorName]
    vendor_tax_id: ExtractedField[Identifier]
    bank_account_iban: ExtractedField[Iban]
    invoice_number: ExtractedField[Identifier]
    po_number: ExtractedField[Identifier]
    currency: ExtractedField[Currency]
    invoice_date: ExtractedField[date]
    due_date: ExtractedField[date]
    subtotal: ExtractedField[Amount] = Field(description="Net subtotal excluding tax")
    tax_amount: ExtractedField[Amount]
    total_amount: ExtractedField[Amount] = Field(description="Gross invoice total including tax")
    line_items: tuple[InvoiceLineItem, ...] = Field(max_length=500)


class ExtractionRequest(DocumentReference):
    run_id: UUID
    invoice_id: UUID
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    scenario: str = Field(default="extraction", pattern=r"^[A-Za-z0-9_\-]{1,64}$")


class ModelUsage(ExtractionModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_total(self) -> Self:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("Token usage total must match input plus output")
        return self


class ModelInvocation(ExtractionModel):
    prompt_version: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=128)
    provider_model: str | None = Field(default=None, max_length=160)
    status: Literal["VALID", "MALFORMED", "FAILED"]
    gateway_attempts: int = Field(ge=0, le=6)
    usage: ModelUsage | None = None
    latency_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cost_usd: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)


class ExtractionOutcome(ExtractionModel):
    run_id: UUID
    invoice_id: UUID
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    calls: tuple[ModelInvocation, ...] = Field(max_length=2)


class ExtractionSuccess(ExtractionOutcome):
    status: Literal["EXTRACTED"] = "EXTRACTED"
    extraction: InvoiceExtraction


EscalationReason = Literal[
    "MALFORMED_MODEL_OUTPUT",
    "INVALID_MODEL_RESPONSE",
    "GATEWAY_UNAVAILABLE",
    "GATEWAY_REJECTED",
    "GUARDRAIL_REJECTED",
    "TOKEN_BUDGET_EXCEEDED",
    "DOCUMENT_UNAVAILABLE",
    "INVALID_DOCUMENT",
    "UNSUPPORTED_DOCUMENT",
]


class ExtractionEscalation(ExtractionOutcome):
    status: Literal["ESCALATED"] = "ESCALATED"
    reason: EscalationReason


ExtractionResult = Annotated[
    ExtractionSuccess | ExtractionEscalation, Field(discriminator="status")
]

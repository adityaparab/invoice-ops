"""Versioned deterministic invoice-validation policy and decision contracts."""

from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from invoiceops_agent.schemas.common import ExactDecimal
from invoiceops_agent.schemas.extraction import InvoiceExtraction

IssueCode = Literal[
    "REQUIRED_FIELD",
    "EMPTY_LINES",
    "NEGATIVE_VALUE",
    "NONPOSITIVE_QUANTITY",
    "TAX_RATE_OUT_OF_RANGE",
    "UNSUPPORTED_CURRENCY",
    "LINE_MATH_MISMATCH",
    "SUBTOTAL_MISMATCH",
    "TAX_MISMATCH",
    "TOTAL_MISMATCH",
]


class ValidationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class CurrencyRule(ValidationModel):
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    decimal_places: int = Field(ge=0, le=4, strict=True)
    absolute_tolerance: ExactDecimal = Field(ge=0, le=100, max_digits=9, decimal_places=4)


class ValidationConfig(ValidationModel):
    """An example regular-invoice policy, independent of any tax jurisdiction."""

    version: str = Field(default="invoice-validation@v1", pattern=r"^[A-Za-z0-9@_.:-]{1,128}$")
    rounding: Literal["ROUND_HALF_UP"] = "ROUND_HALF_UP"
    tax_method: Literal["sum-rounded-line-taxes"] = "sum-rounded-line-taxes"
    currencies: tuple[CurrencyRule, ...] = (
        CurrencyRule(currency="EUR", decimal_places=2, absolute_tolerance=Decimal("0.01")),
        CurrencyRule(currency="GBP", decimal_places=2, absolute_tolerance=Decimal("0.01")),
        CurrencyRule(currency="JPY", decimal_places=0, absolute_tolerance=Decimal("1")),
        CurrencyRule(currency="KWD", decimal_places=3, absolute_tolerance=Decimal("0.001")),
        CurrencyRule(currency="PLN", decimal_places=2, absolute_tolerance=Decimal("0.01")),
        CurrencyRule(currency="USD", decimal_places=2, absolute_tolerance=Decimal("0.01")),
    )

    @model_validator(mode="after")
    def unique_currencies(self) -> Self:
        codes = [rule.currency for rule in self.currencies]
        if not codes or len(codes) > 200 or len(codes) != len(set(codes)):
            raise ValueError("Currency rules must contain 1–200 unique currency codes")
        return self


class ValidationIssue(ValidationModel):
    code: IssueCode
    field: str = Field(pattern=r"^[a-z_]+(?:\.[a-z_]+)?$")
    line_number: int | None = Field(default=None, ge=1, le=500)
    expected: ExactDecimal | None = None
    actual: ExactDecimal | None = None
    difference: ExactDecimal | None = None
    tolerance: ExactDecimal | None = Field(default=None, ge=0)


class ValidationRequest(ValidationModel):
    run_id: UUID
    invoice_id: UUID
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    extraction: InvoiceExtraction = Field(repr=False)


class ValidationResult(ValidationModel):
    status: Literal["PASS", "FAIL"]
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config: ValidationConfig
    issues: tuple[ValidationIssue, ...]

    @model_validator(mode="after")
    def consistent_decision(self) -> Self:
        if (self.status == "PASS") != (not self.issues):
            raise ValueError("PASS requires no validation issues; FAIL requires an issue")
        return self

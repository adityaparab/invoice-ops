"""Versioned three-way comparison inputs, tolerances, and evidence contracts."""

from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from invoiceops_agent.schemas.common import ExactDecimal
from invoiceops_agent.schemas.erp import GoodsReceipt, PurchaseOrder, Vendor
from invoiceops_agent.schemas.extraction import InvoiceExtraction


class MatchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class MatchConfig(MatchModel):
    version: str = Field(default="three-way-match@v1", pattern=r"^[A-Za-z0-9@_.:-]{1,128}$")
    price_absolute_tolerance: ExactDecimal = Field(default=Decimal("0.01"), ge=0, le=100)
    price_relative_tolerance: ExactDecimal = Field(default=Decimal("0.005"), ge=0, le=1)
    quantity_absolute_tolerance: ExactDecimal = Field(default=Decimal(0), ge=0, le=100)
    line_total_absolute_tolerance: ExactDecimal = Field(default=Decimal("0.01"), ge=0, le=100)
    line_total_relative_tolerance: ExactDecimal = Field(default=Decimal("0.005"), ge=0, le=1)
    amount_absolute_tolerance: ExactDecimal = Field(default=Decimal("0.01"), ge=0, le=100)
    amount_relative_tolerance: ExactDecimal = Field(default=Decimal("0.005"), ge=0, le=1)


class ERPSnapshot(MatchModel):
    vendor: Vendor
    purchase_order: PurchaseOrder
    goods_receipts: tuple[GoodsReceipt, ...] = Field(max_length=100)

    @model_validator(mode="after")
    def consistent_relations(self) -> Self:
        if self.vendor.id != self.purchase_order.vendor_id:
            raise ValueError("PO vendor does not match the ERP vendor")
        if any(
            receipt.purchase_order_id != self.purchase_order.id for receipt in self.goods_receipts
        ):
            raise ValueError("Goods receipt belongs to another purchase order")
        numbers = [line.line_number for line in self.purchase_order.lines]
        if not numbers or len(numbers) > 500 or numbers != list(range(1, len(numbers) + 1)):
            raise ValueError("PO lines must be ordered contiguously from one")
        if len({receipt.id for receipt in self.goods_receipts}) != len(self.goods_receipts):
            raise ValueError("Goods receipts must be unique")
        po_lines = {line.line_number: line.sku for line in self.purchase_order.lines}
        for receipt in self.goods_receipts:
            if len({line.line_number for line in receipt.lines}) != len(receipt.lines):
                raise ValueError("Receipt line numbers must be unique")
            if any(po_lines.get(line.line_number) != line.sku for line in receipt.lines):
                raise ValueError("Receipt lines must reference matching purchase order lines")
        return self


class MatchRequest(MatchModel):
    run_id: UUID
    invoice_id: UUID
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    extraction: InvoiceExtraction = Field(repr=False)
    snapshot: ERPSnapshot | None = Field(default=None, repr=False)


CheckStatus = Literal["MATCH", "MISMATCH", "UNKNOWN"]


class IdentityCheck(MatchModel):
    field: Literal["po_number", "vendor_name", "currency", "line_count", "description"]
    line_number: int | None = Field(default=None, gt=0)
    expected: str | None
    actual: str | None
    status: CheckStatus


class NumericCheck(MatchModel):
    field: Literal[
        "unit_price",
        "quantity_ordered",
        "quantity_received",
        "receipt_vs_order",
        "line_total",
        "subtotal_ordered",
        "subtotal_received",
        "receipt_vs_order_total",
    ]
    line_number: int | None = Field(default=None, gt=0)
    rule: Literal["equal", "at_most"]
    expected: ExactDecimal | None
    actual: ExactDecimal | None
    difference: ExactDecimal | None
    tolerance: ExactDecimal | None = Field(ge=0)
    status: CheckStatus


class MatchResult(MatchModel):
    status: Literal["PASS", "FAIL", "INCOMPLETE"]
    snapshot_found: bool
    po_number: str | None
    po_status: str | None
    extraction_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config: MatchConfig
    identity_checks: tuple[IdentityCheck, ...]
    numeric_checks: tuple[NumericCheck, ...]

    @model_validator(mode="after")
    def consistent_decision(self) -> Self:
        statuses = [check.status for check in self.identity_checks] + [
            check.status for check in self.numeric_checks
        ]
        expected = (
            "FAIL"
            if "MISMATCH" in statuses
            else "INCOMPLETE"
            if not self.snapshot_found or "UNKNOWN" in statuses
            else "PASS"
        )
        if self.snapshot_found != (self.snapshot_sha256 is not None):
            raise ValueError("ERP snapshot fingerprint must match snapshot presence")
        if self.status != expected:
            raise ValueError("Match status must follow comparison evidence")
        return self

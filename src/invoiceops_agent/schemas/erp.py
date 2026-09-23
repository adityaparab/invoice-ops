"""Versioned synthetic ERP records and factual matching ground truth."""

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from invoiceops_agent.schemas.common import StrictDecimal


class ERPRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Vendor(ERPRecord):
    id: UUID
    external_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    tax_id: str | None = Field(default=None, max_length=128)
    bank_account_iban: str | None = Field(default=None, max_length=34)
    status: Literal["ACTIVE", "INACTIVE"] = "ACTIVE"
    created_at: AwareDatetime


class PurchaseOrderLine(ERPRecord):
    line_number: int = Field(gt=0)
    sku: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=1000)
    quantity: StrictDecimal = Field(gt=0, max_digits=18, decimal_places=4)
    unit_price: StrictDecimal = Field(ge=0, max_digits=18, decimal_places=2)
    line_total: StrictDecimal = Field(ge=0, max_digits=18, decimal_places=2)


class PurchaseOrder(ERPRecord):
    id: UUID
    vendor_id: UUID
    po_number: str = Field(min_length=1, max_length=128)
    status: Literal["OPEN", "PARTIALLY_RECEIVED", "CLOSED", "CANCELLED"]
    currency: Literal["EUR", "GBP", "USD"]
    issued_on: date
    total_amount: StrictDecimal = Field(ge=0, max_digits=18, decimal_places=2)
    lines: tuple[PurchaseOrderLine, ...] = Field(max_length=500)
    created_at: AwareDatetime


class ReceiptLine(ERPRecord):
    line_number: int = Field(gt=0)
    sku: str = Field(min_length=1, max_length=128)
    received_quantity: StrictDecimal = Field(ge=0, max_digits=18, decimal_places=4)


class GoodsReceipt(ERPRecord):
    id: UUID
    purchase_order_id: UUID
    receipt_number: str = Field(min_length=1, max_length=128)
    received_at: AwareDatetime
    lines: tuple[ReceiptLine, ...] = Field(max_length=500)
    created_at: AwareDatetime


class PurchaseOrderTruth(ERPRecord):
    po_number: str
    vendor_external_id: str
    status: Literal["OPEN", "PARTIALLY_RECEIVED", "CLOSED", "CANCELLED"]
    ordered_total: StrictDecimal
    ordered_quantities: tuple[StrictDecimal, ...]
    received_quantities: tuple[StrictDecimal, ...]
    fully_received: bool


class ERPFixture(ERPRecord):
    version: Literal["synthetic-erp@v1"] = "synthetic-erp@v1"
    seed: int
    vendors: tuple[Vendor, ...]
    purchase_orders: tuple[PurchaseOrder, ...]
    goods_receipts: tuple[GoodsReceipt, ...]
    ground_truth: tuple[PurchaseOrderTruth, ...]

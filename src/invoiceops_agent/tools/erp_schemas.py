"""Versioned synthetic ERP records and factual matching ground truth."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ERPRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Vendor(ERPRecord):
    id: UUID
    external_id: str
    name: str
    tax_id: str
    bank_account_iban: str
    status: Literal["ACTIVE"] = "ACTIVE"
    created_at: datetime


class PurchaseOrderLine(ERPRecord):
    line_number: int = Field(gt=0)
    sku: str
    description: str
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    line_total: Decimal = Field(ge=0)


class PurchaseOrder(ERPRecord):
    id: UUID
    vendor_id: UUID
    po_number: str
    status: Literal["OPEN", "PARTIALLY_RECEIVED", "CLOSED", "CANCELLED"]
    currency: Literal["EUR", "GBP", "USD"]
    issued_on: date
    total_amount: Decimal
    lines: tuple[PurchaseOrderLine, ...]
    created_at: datetime


class ReceiptLine(ERPRecord):
    line_number: int = Field(gt=0)
    sku: str
    received_quantity: Decimal = Field(ge=0)


class GoodsReceipt(ERPRecord):
    id: UUID
    purchase_order_id: UUID
    receipt_number: str
    received_at: datetime
    lines: tuple[ReceiptLine, ...]
    created_at: datetime


class PurchaseOrderTruth(ERPRecord):
    po_number: str
    vendor_external_id: str
    status: Literal["OPEN", "PARTIALLY_RECEIVED", "CLOSED", "CANCELLED"]
    ordered_total: Decimal
    ordered_quantities: tuple[Decimal, ...]
    received_quantities: tuple[Decimal, ...]
    fully_received: bool


class ERPFixture(ERPRecord):
    version: Literal["synthetic-erp@v1"] = "synthetic-erp@v1"
    seed: int
    vendors: tuple[Vendor, ...]
    purchase_orders: tuple[PurchaseOrder, ...]
    goods_receipts: tuple[GoodsReceipt, ...]
    ground_truth: tuple[PurchaseOrderTruth, ...]

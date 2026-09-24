"""Contracts for golden documents, labels, and their ERP snapshot."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from invoiceops_agent.schemas.erp import GoodsReceipt, PurchaseOrder, Vendor

AnomalyCode = Literal[
    "DUP_EXACT",
    "DUP_NEAR",
    "PRICE_MM",
    "QTY_MM",
    "MISSING_PO",
    "BANK_CHANGE",
    "CCY_MM",
    "TAX_ERR",
    "MATH_ERR",
    "STALE_PO",
]
Split = Literal["development", "held_out"]
Origin = Literal["voxel51", "synthetic"]
QualityEffect = Literal["rotation", "skew", "stamp", "faint_print"]


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LineLabel(FrozenModel):
    description: str
    quantity: str
    unit_price: str | None = None
    tax_rate: str | None = None
    line_total: str


class InvoiceLabel(FrozenModel):
    vendor_name: str | None = None
    vendor_tax_id: str | None = None
    bank_account_iban: str | None = None
    invoice_number: str | None = None
    po_number: str | None = None
    currency: str | None = None
    invoice_date: date | None = None
    due_date: date | None = None
    subtotal: str | None = None
    tax_amount: str | None = None
    total_amount: str | None = None
    line_items: tuple[LineLabel, ...] = ()


class GoldenSample(FrozenModel):
    sample_id: str
    split: Split
    origin: Origin
    document_path: str
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_id: str | None = None
    source_path: str | None = None
    source_sha256: str | None = None
    parent_id: str | None = None
    effect: QualityEffect | None = None
    quality_tier: Literal["A", "B", "C"]
    label: InvoiceLabel
    anomaly_codes: tuple[AnomalyCode, ...] = ()
    routing_eligible: bool


class GoldenManifest(FrozenModel):
    version: Literal["golden/v1.0.0"] = "golden/v1.0.0"
    generator_version: Literal["golden-builder@v1"] = "golden-builder@v1"
    seed: int
    source_revision: str
    source_metadata_sha256: str
    baseline_manifest_sha256: str
    pillow_version: str
    zlib_version: str | None
    samples: tuple[GoldenSample, ...]

    @model_validator(mode="after")
    def check_distribution(self) -> "GoldenManifest":
        ids = [sample.sample_id for sample in self.samples]
        if len(ids) != len(set(ids)) or len(ids) != 500:
            raise ValueError("Golden corpus requires 500 unique sample IDs")
        clean = [sample for sample in self.samples if not sample.anomaly_codes]
        anomalous = [sample for sample in self.samples if sample.anomaly_codes]
        if len(clean) != 350 or len(anomalous) != 150:
            raise ValueError("Golden corpus requires 350 clean and 150 anomalous samples")
        if sum(sample.split == "development" for sample in self.samples) != 100:
            raise ValueError("Development split requires 100 samples")
        if sum(sample.effect is not None for sample in clean) != 30:
            raise ValueError("Clean set requires 30 quality hard negatives")
        return self


class GoldenERP(FrozenModel):
    version: Literal["golden-erp@v1"] = "golden-erp@v1"
    seed: int
    vendors: tuple[Vendor, ...]
    purchase_orders: tuple[PurchaseOrder, ...]
    goods_receipts: tuple[GoodsReceipt, ...]


class BaselineSource(FrozenModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    sample_id: str
    source_path: str
    source_sha256: str


class BaselineReport(FrozenModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    manifest_sha256: str
    samples: tuple[BaselineSource, ...]

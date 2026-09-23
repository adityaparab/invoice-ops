"""Explicit annotation mapping and exact-value field F1 for the development subset."""

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

from invoiceops_agent.schemas.extraction import InvoiceExtraction

Tier = Literal["A", "B", "C"]
TIERS: tuple[Tier, ...] = ("A", "B", "C")
FIELDS = (
    "vendor_name",
    "invoice_number",
    "invoice_date",
    "tax_amount",
    "line_description",
    "line_quantity",
    "line_total",
)
SCORING_VERSION = "voxel51-field-f1@v1"


class LabelModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class InvoiceLabels(LabelModel):
    seller_name: str
    invoice_number: str
    invoice_date: str


class ItemLabels(LabelModel):
    description: str
    quantity: str
    total_price: str


class SubtotalLabels(LabelModel):
    tax: str


class Annotation(LabelModel):
    invoice: InvoiceLabels
    items: tuple[ItemLabels, ...]
    subtotal: SubtotalLabels


class Quality(LabelModel):
    tier: Tier
    version: str


class Sample(LabelModel):
    sample_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    prepared_path: str
    prepared_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    quality: Quality
    annotation: Annotation


class Manifest(LabelModel):
    revision: str
    metadata_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pipeline_version: str
    samples: tuple[Sample, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_samples(self) -> "Manifest":
        identities = [sample.sample_id for sample in self.samples]
        if len(identities) != len(set(identities)):
            raise ValueError("Baseline sample identities must be unique")
        return self


@dataclass(frozen=True)
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    def __add__(self, other: "Counts") -> "Counts":
        return Counts(self.tp + other.tp, self.fp + other.fp, self.fn + other.fn)

    def summary(self) -> dict[str, int | str]:
        precision = Decimal(self.tp) / (self.tp + self.fp) if self.tp + self.fp else Decimal(0)
        recall = Decimal(self.tp) / (self.tp + self.fn) if self.tp + self.fn else Decimal(0)
        denominator = 2 * self.tp + self.fp + self.fn
        f1 = Decimal(2 * self.tp) / denominator if denominator else Decimal(0)
        return {
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "eligible": self.tp + self.fn,
            "precision": format(precision.quantize(Decimal("0.0001")), "f"),
            "recall": format(recall.quantize(Decimal("0.0001")), "f"),
            "f1": format(f1.quantize(Decimal("0.0001")), "f"),
        }


class TierSummary(TypedDict):
    sample_count: int
    overall: dict[str, int | str]
    fields: dict[str, dict[str, int | str]]


def _text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _money(value: str) -> str:
    compact = re.sub(r"[\s\u00a0\u202f$€£]", "", value)
    if "," in compact and "." in compact:
        if compact.rfind(",") > compact.rfind("."):
            compact = compact.replace(".", "").replace(",", ".")
        else:
            compact = compact.replace(",", "")
    elif "," in compact:
        head, tail = compact.rsplit(",", 1)
        compact = compact.replace(",", "") if len(tail) == 3 else head.replace(",", "") + "." + tail
    try:
        return format(Decimal(compact).normalize(), "f")
    except InvalidOperation as error:
        raise ValueError("Invalid annotated monetary value") from error


def _quantity(value: str) -> str:
    try:
        return format(Decimal(value.strip().replace(",", ".")).normalize(), "f")
    except InvalidOperation as error:
        raise ValueError("Invalid annotated quantity") from error


def _date(value: str) -> str:
    try:
        return datetime.strptime(value.strip(), "%m/%d/%Y").date().isoformat()
    except ValueError as error:
        raise ValueError("Invalid annotated invoice date") from error


def _compare(expected: str, actual: str | None) -> Counts:
    if not expected:
        return Counts()
    if actual is None:
        return Counts(fn=1)
    if expected == actual:
        return Counts(tp=1)
    return Counts(fp=1, fn=1)


def score_sample(sample: Sample, extraction: InvoiceExtraction | None) -> dict[str, Counts]:
    """Score only annotated fields; wrong values count as one FP and one FN."""
    labels = sample.annotation
    results = {field: Counts() for field in FIELDS}
    predicted = extraction
    results["vendor_name"] = _compare(
        _text(labels.invoice.seller_name),
        _text(predicted.vendor_name.value)
        if predicted is not None and predicted.vendor_name.value is not None
        else None,
    )
    results["invoice_number"] = _compare(
        _text(labels.invoice.invoice_number),
        _text(predicted.invoice_number.value)
        if predicted is not None and predicted.invoice_number.value is not None
        else None,
    )
    results["invoice_date"] = _compare(
        _date(labels.invoice.invoice_date),
        predicted.invoice_date.value.isoformat()
        if predicted is not None and predicted.invoice_date.value is not None
        else None,
    )
    results["tax_amount"] = _compare(
        _money(labels.subtotal.tax),
        _quantity(str(predicted.tax_amount.value))
        if predicted is not None and predicted.tax_amount.value is not None
        else None,
    )
    observed = predicted.line_items if predicted is not None else ()
    for index, item in enumerate(labels.items):
        line = observed[index] if index < len(observed) else None
        values = {
            "line_description": (
                _text(item.description),
                _text(line.description.value)
                if line is not None and line.description.value is not None
                else None,
            ),
            "line_quantity": (
                _quantity(item.quantity),
                _quantity(str(line.quantity.value))
                if line is not None and line.quantity.value is not None
                else None,
            ),
            "line_total": (
                _money(item.total_price),
                _quantity(str(line.line_total.value))
                if line is not None and line.line_total.value is not None
                else None,
            ),
        }
        for field, (expected, actual) in values.items():
            results[field] += _compare(expected, actual)
    for line in observed[len(labels.items) :]:
        if line.description.value is not None:
            results["line_description"] += Counts(fp=1)
        if line.quantity.value is not None:
            results["line_quantity"] += Counts(fp=1)
        if line.line_total.value is not None:
            results["line_total"] += Counts(fp=1)
    return results


def summarize(scores: list[tuple[Tier, dict[str, Counts]]]) -> dict[Tier, TierSummary | None]:
    by_tier: dict[Tier, TierSummary | None] = {}
    for tier in TIERS:
        matching = [score for scored_tier, score in scores if scored_tier == tier]
        if not matching:
            by_tier[tier] = None
            continue
        totals = {field: Counts() for field in FIELDS}
        for score in matching:
            for field in FIELDS:
                totals[field] += score[field]
        overall = Counts()
        for count in totals.values():
            overall += count
        by_tier[tier] = {
            "sample_count": len(matching),
            "overall": overall.summary(),
            "fields": {field: totals[field].summary() for field in FIELDS},
        }
    return by_tier

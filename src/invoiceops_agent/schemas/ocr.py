"""Auditable, bounded OCR observations for printed invoice identifiers."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class OCRContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class OCRIdentifier(OCRContract):
    value: str = Field(min_length=1, max_length=128)
    confidence: Decimal = Field(ge=0, le=100)


class OCRIdentifiers(OCRContract):
    version: Literal["identifier-ocr@v1"] = "identifier-ocr@v1"
    status: Literal["COMPLETE", "SKIPPED", "UNAVAILABLE"]
    engine_version: str | None = Field(default=None, max_length=80)
    bank_account_iban: OCRIdentifier | None = None
    po_number: OCRIdentifier | None = None

"""Versioned ten-code exception taxonomy and auditable evidence references."""

from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from invoiceops_agent.schemas.common import model_digest
from invoiceops_agent.schemas.extraction import InvoiceExtraction
from invoiceops_agent.schemas.matching import MatchResult
from invoiceops_agent.schemas.validation import ValidationResult

ExceptionCode = Literal[
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
CODE_ORDER: tuple[ExceptionCode, ...] = (
    "DUP_EXACT",
    "DUP_NEAR",
    "MISSING_PO",
    "BANK_CHANGE",
    "CCY_MM",
    "PRICE_MM",
    "QTY_MM",
    "TAX_ERR",
    "MATH_ERR",
    "STALE_PO",
)


class TaxonomyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class TaxonomyConfig(TaxonomyModel):
    version: str = Field(default="exception-taxonomy@v1", pattern=r"^[A-Za-z0-9@_.:-]{1,128}$")


class TaxonomyRequest(TaxonomyModel):
    run_id: UUID
    invoice_id: UUID
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    extraction: InvoiceExtraction | None = Field(default=None, repr=False)
    validation: ValidationResult | None = Field(default=None, repr=False)
    match: MatchResult | None = Field(default=None, repr=False)
    vendor_bank_iban: str | None = Field(default=None, max_length=64, repr=False)
    exact_duplicate: bool = False
    near_duplicate: bool = False
    stale_po: bool = False

    @model_validator(mode="after")
    def consistent_provenance(self) -> Self:
        if self.extraction is None and not (
            self.exact_duplicate or self.near_duplicate or self.stale_po
        ):
            raise ValueError("Taxonomy requires extraction or an external exception signal")
        if (self.validation is not None or self.match is not None) and self.extraction is None:
            raise ValueError("Validation and match evidence require the extraction input")
        if self.extraction is not None:
            digest = model_digest(self.extraction)
            if self.validation is not None and self.validation.input_sha256 != digest:
                raise ValueError("Validation evidence does not match the extraction")
            if self.match is not None and self.match.extraction_sha256 != digest:
                raise ValueError("Match evidence does not match the extraction")
        return self


EvidenceSource = Literal[
    "INGEST", "SIMILARITY", "MATCH_IDENTITY", "MATCH_NUMERIC", "VALIDATION", "ERP", "POLICY"
]


class EvidenceRef(TaxonomyModel):
    source: EvidenceSource
    field: str = Field(min_length=1, max_length=80)
    line_number: int | None = Field(default=None, gt=0)
    source_index: int | None = Field(default=None, ge=0)


class ExceptionFinding(TaxonomyModel):
    code: ExceptionCode
    evidence: EvidenceRef


class TaxonomyResult(TaxonomyModel):
    status: Literal["CLEAN", "EXCEPTION", "UNCLASSIFIED"]
    codes: tuple[ExceptionCode, ...]
    findings: tuple[ExceptionFinding, ...]
    unresolved: tuple[EvidenceRef, ...]
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    extraction_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    validation_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    match_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    vendor_bank_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config: TaxonomyConfig

    @model_validator(mode="after")
    def consistent_result(self) -> Self:
        found = {finding.code for finding in self.findings}
        expected_codes = tuple(code for code in CODE_ORDER if code in found)
        if self.codes != expected_codes:
            raise ValueError("Exception codes must be unique and in taxonomy order")
        expected_status = (
            "EXCEPTION" if self.findings else "UNCLASSIFIED" if self.unresolved else "CLEAN"
        )
        if self.status != expected_status:
            raise ValueError("Taxonomy status does not match its findings")
        return self

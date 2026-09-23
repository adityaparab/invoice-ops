"""Versioned deterministic spend, approval, and purchase-order policy contracts."""

from datetime import date
from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from invoiceops_agent.schemas.common import ExactDecimal, model_digest
from invoiceops_agent.schemas.exceptions import TaxonomyResult
from invoiceops_agent.schemas.extraction import InvoiceExtraction
from invoiceops_agent.schemas.matching import ERPSnapshot, MatchResult
from invoiceops_agent.schemas.validation import ValidationResult

ApprovalTier = Literal["NONE", "AP_MANAGER", "PROCUREMENT_DIRECTOR", "DUAL_CONTROL", "UNKNOWN"]
PolicyStatus = Literal["AUTO_APPROVE_ELIGIBLE", "REVIEW", "BLOCK"]
PolicyReason = Literal[
    "EXACT_DUPLICATE",
    "CANCELLED_PO",
    "CLOSED_PO",
    "STALE_PO",
    "FUTURE_PO",
    "SPEND_CAP_EXCEEDED",
    "APPROVAL_REQUIRED",
    "EXCEPTION_PRESENT",
    "UNRESOLVED_EVIDENCE",
    "VALIDATION_FAILED",
    "MATCH_NOT_PASSED",
    "UNKNOWN_AMOUNT",
    "INVALID_AMOUNT",
    "UNSUPPORTED_CURRENCY",
]


class PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class SpendBand(PolicyModel):
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    auto_approve_limit: ExactDecimal = Field(default=Decimal("10000"), ge=0)
    manager_limit: ExactDecimal = Field(default=Decimal("50000"), gt=0)
    director_limit: ExactDecimal = Field(default=Decimal("100000"), gt=0)
    hard_spend_limit: ExactDecimal = Field(default=Decimal("250000"), gt=0)

    @model_validator(mode="after")
    def ascending_bands(self) -> Self:
        if not (
            self.auto_approve_limit
            < self.manager_limit
            < self.director_limit
            < self.hard_spend_limit
        ):
            raise ValueError("Spend and approval limits must ascend strictly")
        return self


class PolicyConfig(PolicyModel):
    version: str = Field(default="invoice-policy@v1", pattern=r"^[A-Za-z0-9@_.:-]{1,128}$")
    bands: tuple[SpendBand, ...] = (
        SpendBand(currency="EUR"),
        SpendBand(currency="GBP"),
        SpendBand(
            currency="JPY",
            auto_approve_limit=Decimal("1000000"),
            manager_limit=Decimal("5000000"),
            director_limit=Decimal("10000000"),
            hard_spend_limit=Decimal("25000000"),
        ),
        SpendBand(
            currency="KWD",
            auto_approve_limit=Decimal("3000"),
            manager_limit=Decimal("15000"),
            director_limit=Decimal("30000"),
            hard_spend_limit=Decimal("75000"),
        ),
        SpendBand(
            currency="PLN",
            auto_approve_limit=Decimal("40000"),
            manager_limit=Decimal("200000"),
            director_limit=Decimal("400000"),
            hard_spend_limit=Decimal("1000000"),
        ),
        SpendBand(currency="USD"),
    )
    max_po_age_days: int = Field(default=365, ge=1, le=3650)

    @model_validator(mode="after")
    def unique_currencies(self) -> Self:
        codes = [band.currency for band in self.bands]
        if not codes or len(codes) > 200 or len(codes) != len(set(codes)):
            raise ValueError("Spend bands must contain 1–200 unique currencies")
        return self


class PolicyRequest(PolicyModel):
    run_id: UUID
    invoice_id: UUID
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    as_of: date
    extraction: InvoiceExtraction = Field(repr=False)
    validation: ValidationResult = Field(repr=False)
    match: MatchResult = Field(repr=False)
    taxonomy: TaxonomyResult = Field(repr=False)
    snapshot: ERPSnapshot | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def evidence_is_same_invoice(self) -> Self:
        extraction_hash = model_digest(self.extraction)
        if self.validation.input_sha256 != extraction_hash:
            raise ValueError("Validation evidence does not match the policy extraction")
        if self.match.extraction_sha256 != extraction_hash:
            raise ValueError("Match evidence does not match the policy extraction")
        if self.taxonomy.extraction_sha256 != extraction_hash:
            raise ValueError("Taxonomy evidence does not match the policy extraction")
        if self.taxonomy.validation_sha256 != model_digest(self.validation):
            raise ValueError("Taxonomy validation fingerprint does not match")
        if self.taxonomy.match_sha256 != model_digest(self.match):
            raise ValueError("Taxonomy match fingerprint does not match")
        if self.match.snapshot_found != (self.snapshot is not None):
            raise ValueError("ERP snapshot presence does not match the match result")
        if self.snapshot is not None and self.match.snapshot_sha256 != model_digest(self.snapshot):
            raise ValueError("ERP snapshot does not match the comparison evidence")
        if self.snapshot is not None and (
            self.match.po_status != self.snapshot.purchase_order.status
            or self.match.po_number != self.snapshot.purchase_order.po_number
        ):
            raise ValueError("Matched purchase order identity does not match the ERP snapshot")
        return self


class PolicyFinding(PolicyModel):
    reason: PolicyReason
    field: str = Field(min_length=1, max_length=80)


class PolicyResult(PolicyModel):
    status: PolicyStatus
    approval_tier: ApprovalTier
    currency: str | None
    amount: ExactDecimal | None
    findings: tuple[PolicyFinding, ...]
    as_of: date
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config: PolicyConfig

    @model_validator(mode="after")
    def decision_matches_findings(self) -> Self:
        reasons = {finding.reason for finding in self.findings}
        if len(reasons) != len(self.findings):
            raise ValueError("Policy findings must have unique reasons")
        blocked = bool(reasons & {"EXACT_DUPLICATE", "CANCELLED_PO", "SPEND_CAP_EXCEEDED"})
        expected: PolicyStatus = (
            "BLOCK" if blocked else "REVIEW" if self.findings else "AUTO_APPROVE_ELIGIBLE"
        )
        if self.status != expected:
            raise ValueError("Policy status must follow its findings")
        if self.status == "AUTO_APPROVE_ELIGIBLE" and (
            self.approval_tier != "NONE"
            or self.amount is None
            or self.amount < 0
            or self.currency is None
        ):
            raise ValueError("Auto eligibility requires a known valid amount and no approver")
        return self

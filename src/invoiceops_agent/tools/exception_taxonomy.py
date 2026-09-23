"""Classify existing deterministic evidence into the versioned exception codes."""

from hashlib import sha256

from invoiceops_agent.schemas.common import model_digest
from invoiceops_agent.schemas.exceptions import (
    CODE_ORDER,
    EvidenceRef,
    ExceptionCode,
    ExceptionFinding,
    TaxonomyConfig,
    TaxonomyRequest,
    TaxonomyResult,
)

_TAX_ISSUES = frozenset({"TAX_MISMATCH", "TAX_RATE_OUT_OF_RANGE"})
_MATH_ISSUES = frozenset(
    {
        "LINE_MATH_MISMATCH",
        "SUBTOTAL_MISMATCH",
        "TOTAL_MISMATCH",
        "NEGATIVE_VALUE",
        "NONPOSITIVE_QUANTITY",
    }
)
_QUANTITY_FIELDS = frozenset(
    {"quantity_ordered", "quantity_received", "receipt_vs_order", "receipt_vs_order_total"}
)


def _iban(value: str) -> str:
    return "".join(value.split()).upper()


def classify_exceptions(
    request: TaxonomyRequest, config: TaxonomyConfig | None = None
) -> TaxonomyResult:
    """Use only supplied evidence; unknown or unmapped failures remain unresolved."""
    policy = config if config is not None else TaxonomyConfig()
    findings: list[ExceptionFinding] = []
    unresolved: list[EvidenceRef] = []

    def add(code: ExceptionCode, evidence: EvidenceRef) -> None:
        findings.append(ExceptionFinding(code=code, evidence=evidence))

    if request.exact_duplicate:
        add("DUP_EXACT", EvidenceRef(source="INGEST", field="content_hash"))
    if request.near_duplicate:
        add("DUP_NEAR", EvidenceRef(source="SIMILARITY", field="invoice_similarity"))

    match = request.match
    if match is not None:
        if not match.snapshot_found:
            add("MISSING_PO", EvidenceRef(source="MATCH_IDENTITY", field="po_number"))
        if match.po_status in {"CLOSED", "CANCELLED"}:
            add("STALE_PO", EvidenceRef(source="ERP", field="po_status"))
        for index, check in enumerate(match.identity_checks):
            if check.status == "MATCH":
                continue
            evidence = EvidenceRef(
                source="MATCH_IDENTITY",
                field=check.field,
                line_number=check.line_number,
                source_index=index,
            )
            if check.status == "MISMATCH" and check.field == "currency":
                add("CCY_MM", evidence)
            else:
                unresolved.append(evidence)
        for index, numeric_check in enumerate(match.numeric_checks):
            if numeric_check.status == "MATCH":
                continue
            evidence = EvidenceRef(
                source="MATCH_NUMERIC",
                field=numeric_check.field,
                line_number=numeric_check.line_number,
                source_index=index,
            )
            if numeric_check.status == "MISMATCH" and numeric_check.field == "unit_price":
                add("PRICE_MM", evidence)
            elif numeric_check.status == "MISMATCH" and numeric_check.field in _QUANTITY_FIELDS:
                add("QTY_MM", evidence)
            else:
                unresolved.append(evidence)
    elif request.extraction is not None:
        unresolved.append(EvidenceRef(source="MATCH_IDENTITY", field="not_run"))

    validation = request.validation
    if validation is not None:
        for index, issue in enumerate(validation.issues):
            evidence = EvidenceRef(
                source="VALIDATION",
                field=issue.field,
                line_number=issue.line_number,
                source_index=index,
            )
            if issue.code in _TAX_ISSUES:
                add("TAX_ERR", evidence)
            elif issue.code in _MATH_ISSUES:
                add("MATH_ERR", evidence)
            else:
                unresolved.append(evidence)
    elif request.extraction is not None:
        unresolved.append(EvidenceRef(source="VALIDATION", field="not_run"))

    if request.extraction is not None:
        observed_bank = request.extraction.bank_account_iban.value
        if not observed_bank or not request.vendor_bank_iban:
            unresolved.append(EvidenceRef(source="ERP", field="bank_account_iban"))
        elif not _iban(observed_bank) or not _iban(request.vendor_bank_iban):
            unresolved.append(EvidenceRef(source="ERP", field="bank_account_iban"))
        elif _iban(observed_bank) != _iban(request.vendor_bank_iban):
            add("BANK_CHANGE", EvidenceRef(source="ERP", field="bank_account_iban"))
    if request.stale_po:
        stale_ref = EvidenceRef(source="POLICY", field="stale_po")
        if not any(finding.code == "STALE_PO" for finding in findings):
            add("STALE_PO", stale_ref)

    codes = tuple(code for code in CODE_ORDER if any(item.code == code for item in findings))
    status = "EXCEPTION" if findings else "UNCLASSIFIED" if unresolved else "CLEAN"
    expected_bank = _iban(request.vendor_bank_iban) if request.vendor_bank_iban else None
    return TaxonomyResult(
        status=status,
        codes=codes,
        findings=tuple(findings),
        unresolved=tuple(unresolved),
        input_sha256=model_digest(request),
        extraction_sha256=model_digest(request.extraction) if request.extraction else None,
        validation_sha256=model_digest(validation) if validation else None,
        match_sha256=model_digest(match) if match else None,
        vendor_bank_sha256=(
            sha256(expected_bank.encode("utf-8")).hexdigest() if expected_bank else None
        ),
        config_sha256=model_digest(policy),
        config=policy,
    )

"""Anchored OCR must reject weak or malformed identifiers before reconciliation."""

from decimal import Decimal

import pytest
from tests.unit.extraction_support import invoice_extraction

from invoiceops_agent.schemas.ocr import OCRIdentifier, OCRIdentifiers
from invoiceops_agent.tools.ocr_identifiers import parse_tesseract_tsv, reconcile_identifiers

pytestmark = pytest.mark.unit


def _tsv(lines: list[tuple[int, int, str, str]]) -> bytes:
    rows = [
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
    ]
    rows.extend(
        f"5\t1\t1\t1\t{line}\t{word}\t0\t0\t100\t20\t{confidence}\t{text}"
        for line, word, confidence, text in lines
    )
    return ("\n".join(rows) + "\n").encode()


def test_tesseract_parser_reads_high_confidence_anchored_bank_and_po() -> None:
    result = parse_tesseract_tsv(
        _tsv(
            [
                (1, 1, "93", "Bank"),
                (1, 2, "92", "IBAN:"),
                (1, 3, "76", "GBOOSYNTH00202608270002"),
                (2, 1, "93", "PO:"),
                (2, 2, "90", "SYN-GOLD-20260827-PO-SYN-CLEAN-0062"),
            ]
        )
    )
    assert result.bank_account_iban == OCRIdentifier(value="GB00SYNTH00202608270002", confidence=76)
    assert result.po_number == OCRIdentifier(
        value="SYN-GOLD-20260827-PO-SYN-CLEAN-0062", confidence=90
    )


def test_tesseract_parser_rejects_low_confidence_or_duplicate_label() -> None:
    result = parse_tesseract_tsv(
        _tsv(
            [
                (1, 1, "93", "Bank"),
                (1, 2, "92", "IBAN:"),
                (1, 3, "85", "GB00SYNTH00202608270002"),
                (2, 1, "93", "Bank"),
                (2, 2, "92", "IBAN:"),
                (2, 3, "85", "GB00SYNTH00202608279999"),
                (3, 1, "93", "PO:"),
                (3, 2, "56", "SYN-GOLD-20260827-PO-SYN-CLEAN-0001"),
            ]
        )
    )
    assert result.bank_account_iban is None
    assert result.po_number is None


def test_reconciliation_preserves_model_confidence_ceiling_and_missing_fields() -> None:
    extraction = invoice_extraction().model_copy(
        update={
            "bank_account_iban": invoice_extraction().bank_account_iban.model_copy(
                update={"value": "GB00SYNTH0020260827002", "confidence": Decimal("1")}
            ),
        }
    )
    observations = OCRIdentifiers(
        status="COMPLETE",
        bank_account_iban=OCRIdentifier(value="GB00SYNTH00202608270002", confidence=76),
        po_number=OCRIdentifier(value="SYN-PO-1", confidence=90),
    )
    corrected, applied = reconcile_identifiers(extraction, observations)
    assert applied == ("bank_account_iban",)
    assert corrected.bank_account_iban.value == "GB00SYNTH00202608270002"
    assert corrected.bank_account_iban.confidence == Decimal("0.95")
    assert corrected.po_number.value is None

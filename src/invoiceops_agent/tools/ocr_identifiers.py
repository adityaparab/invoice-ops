"""Read labeled identifiers with bounded Tesseract subprocesses and no network."""

import asyncio
import csv
import io
import logging
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from time import perf_counter
from uuid import UUID

from invoiceops_agent.schemas.extraction import (
    ExtractedField,
    Iban,
    Identifier,
    InvoiceExtraction,
)
from invoiceops_agent.schemas.ocr import OCRIdentifier, OCRIdentifiers
from invoiceops_agent.tools.ingestion_schemas import RawDocument

logger = logging.getLogger(__name__)
OCR_VERSION = "identifier-ocr@v1"
MIN_CONFIDENCE = Decimal("70")
MAX_OUTPUT_BYTES = 1_000_000
TIMEOUT_SECONDS = 5
IBAN_PATTERN = re.compile(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$")
PO_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


def _identifier(words: list[tuple[str, Decimal]], kind: str) -> OCRIdentifier | None:
    tokens = [text for text, _ in words]
    if kind == "bank":
        if len(tokens) < 3 or [value.casefold().rstrip(":") for value in tokens[:2]] != [
            "bank",
            "iban",
        ]:
            return None
        values = words[2:]
        raw = "".join(value for value, _ in values).upper()
        if len(raw) >= 4:
            raw = raw[:2] + raw[2:4].replace("O", "0") + raw[4:]
        if not IBAN_PATTERN.fullmatch(raw) or len(raw) > 64:
            return None
    else:
        if len(tokens) < 2 or tokens[0].casefold().rstrip(":") != "po":
            return None
        values = words[1:]
        raw = "".join(value for value, _ in values)
        if not PO_PATTERN.fullmatch(raw):
            return None
    confidence = min(score for _, score in values)
    return OCRIdentifier(value=raw, confidence=confidence) if confidence >= MIN_CONFIDENCE else None


def parse_tesseract_tsv(data: bytes) -> OCRIdentifiers:
    """Accept only one high-confidence anchored line per identifier."""
    if len(data) > MAX_OUTPUT_BYTES:
        raise ValueError("OCR output exceeds the configured bound")
    lines: dict[tuple[str, str, str, str], list[tuple[str, Decimal]]] = defaultdict(list)
    for row in csv.DictReader(io.StringIO(data.decode("utf-8")), delimiter="\t"):
        if row.get("level") != "5" or not (text := row.get("text", "").strip()):
            continue
        try:
            confidence = Decimal(row["conf"])
            if not confidence.is_finite() or confidence < 0 or confidence > 100:
                continue
            key = (
                row["page_num"],
                row["block_num"],
                row["par_num"],
                row["line_num"],
            )
        except (InvalidOperation, KeyError):
            continue
        lines[key].append((text, confidence))
    candidates: dict[str, list[OCRIdentifier]] = {"bank": [], "po": []}
    for words in lines.values():
        for kind in candidates:
            if found := _identifier(words, kind):
                candidates[kind].append(found)
    return OCRIdentifiers(
        status="COMPLETE",
        bank_account_iban=candidates["bank"][0] if len(candidates["bank"]) == 1 else None,
        po_number=candidates["po"][0] if len(candidates["po"]) == 1 else None,
    )


async def _communicate(
    process: asyncio.subprocess.Process, data: bytes | None, limit_seconds: float
) -> bytes:
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(data), timeout=limit_seconds)
        return stdout
    except (TimeoutError, asyncio.CancelledError):
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        raise


class TesseractIdentifierOCR:
    async def _engine_version(self) -> str | None:
        process = await asyncio.create_subprocess_exec(
            "tesseract",
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout = await _communicate(process, None, 1)
        version = stdout.splitlines()[0].decode("ascii") if stdout else ""
        return (
            version[:80] if process.returncode == 0 and version.startswith("tesseract ") else None
        )

    async def read(self, document: RawDocument, *, run_id: UUID, trace_id: str) -> OCRIdentifiers:
        if document.content_type not in {"image/png", "image/jpeg"}:
            return OCRIdentifiers(status="SKIPPED")
        started = perf_counter()
        try:
            engine_version = await self._engine_version()
            if engine_version is None:
                raise ValueError("OCR engine version unavailable")
            process = await asyncio.create_subprocess_exec(
                "tesseract",
                "stdin",
                "stdout",
                "--psm",
                "6",
                "tsv",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout = await _communicate(process, document.body, TIMEOUT_SECONDS)
            if process.returncode != 0:
                raise ValueError("OCR process exited unsuccessfully")
            result = parse_tesseract_tsv(stdout).model_copy(
                update={"engine_version": engine_version}
            )
        except (OSError, TimeoutError, UnicodeError, ValueError):
            result = OCRIdentifiers(status="UNAVAILABLE")
        logger.info(
            "identifier_ocr_completed run_id=%s trace_id=%s status=%s "
            "bank_found=%s po_found=%s duration_ms=%.3f",
            run_id,
            trace_id,
            result.status,
            result.bank_account_iban is not None,
            result.po_number is not None,
            (perf_counter() - started) * 1000,
        )
        return result


def reconcile_identifiers(
    extraction: InvoiceExtraction, observations: OCRIdentifiers
) -> tuple[InvoiceExtraction, tuple[str, ...]]:
    """Use exact OCR text when the model already identified the field."""
    if observations.status != "COMPLETE":
        return extraction, ()
    updates: dict[str, object] = {}
    applied: list[str] = []
    for field in ("bank_account_iban", "po_number"):
        observed = getattr(observations, field)
        current = getattr(extraction, field)
        if observed is None or current.value is None or observed.value == current.value:
            continue
        confidence = min(current.confidence, Decimal("0.95"))
        if field == "bank_account_iban":
            updates[field] = ExtractedField[Iban](value=observed.value, confidence=confidence)
        else:
            updates[field] = ExtractedField[Identifier](value=observed.value, confidence=confidence)
        applied.append(field)
    return extraction.model_copy(update=updates), tuple(applied)

"""Synthetic invoice data shared by offline extraction and document tests."""

import base64
import hashlib
from io import BytesIO
from uuid import UUID

from PIL import Image
from pypdf import PdfWriter

from invoiceops_agent.schemas.extraction import ExtractionRequest, InvoiceExtraction
from invoiceops_agent.tools.ingestion_schemas import DocumentType, RawDocument

SYNTHETIC_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAI0lEQVR4nGP8//8/AymAiSTVDKMaiANMRKqDg1ENxACSQwkAVW0DHeN02ZEAAAAASUVORK5CYII="
)

RUN_ID = UUID(int=1)
INVOICE_ID = UUID(int=2)
TRACE_ID = "a" * 32


def png_bytes(width: int = 16, height: int = 16) -> bytes:
    output = BytesIO()
    with Image.new("RGB", (width, height), "white") as image:
        image.save(output, format="PNG")
    return output.getvalue()


def pdf_bytes(*, pages: int = 1, width: int = 100, height: int = 100) -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=width, height=height)
    writer.write(output)
    writer.close()
    return output.getvalue()


def raw_document(body: bytes | None = None, media_type: DocumentType = "image/png") -> RawDocument:
    body = SYNTHETIC_PNG if body is None else body
    digest = hashlib.sha256(body).hexdigest()
    return RawDocument(media_type, digest, "0" * 64, body)


def extraction_request(
    document: RawDocument | None = None, *, scenario: str = "synthetic_invoice"
) -> ExtractionRequest:
    document = raw_document() if document is None else document
    return ExtractionRequest(
        run_id=RUN_ID,
        invoice_id=INVOICE_ID,
        trace_id=TRACE_ID,
        raw_ref=f"s3://invoiceops-raw/{document.object_key}",
        content_hash=document.content_hash,
        content_type=document.content_type,
        scenario=scenario,
    )


def invoice_extraction() -> InvoiceExtraction:
    return InvoiceExtraction.model_validate(
        {
            "vendor_name": {"value": "Synthetic Supplier 001", "confidence": "0.9"},
            "vendor_tax_id": {"value": None, "confidence": "0"},
            "bank_account_iban": {"value": None, "confidence": "0"},
            "invoice_number": {"value": "SYN-001", "confidence": "0.9"},
            "po_number": {"value": None, "confidence": "0"},
            "currency": {"value": "USD", "confidence": "1"},
            "invoice_date": {"value": "2026-09-23", "confidence": "1"},
            "due_date": {"value": None, "confidence": "0"},
            "subtotal": {"value": "100.00", "confidence": "1"},
            "tax_amount": {"value": "20.00", "confidence": "1"},
            "total_amount": {"value": "120.00", "confidence": "1"},
            "line_items": [
                {
                    "description": {"value": "Synthetic service", "confidence": "1"},
                    "quantity": {"value": "2", "confidence": "1"},
                    "unit_price": {"value": "50.00", "confidence": "1"},
                    "tax_rate": {"value": "0.20", "confidence": "1"},
                    "line_total": {"value": "100.00", "confidence": "1"},
                }
            ],
        }
    )

"""Bound decoded geometry off the event loop; preserve the immutable source representation."""

import asyncio
import logging
from decimal import Decimal, DecimalException
from io import BytesIO
from time import monotonic, perf_counter
from uuid import UUID

from PIL import Image
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from invoiceops_agent.tools.document_errors import DocumentError, DocumentUnavailable
from invoiceops_agent.tools.document_settings import DocumentSettings
from invoiceops_agent.tools.ingestion_schemas import RawDocument

logger = logging.getLogger(__name__)
PREFLIGHT_VERSION = "document-preflight@v1"


def _check_deadline(deadline: float) -> None:
    if monotonic() >= deadline:
        raise TimeoutError("Document preflight deadline exceeded")


def _check_pdf(body: bytes, settings: DocumentSettings, deadline: float) -> None:
    with PdfReader(BytesIO(body), strict=True) as reader:
        if reader.is_encrypted:
            raise ValueError("Encrypted PDF is unsupported")
        objects = sum(len(entries) for entries in reader.xref.values()) + len(reader.xref_objStm)
        if objects > settings.max_pdf_objects:
            raise ValueError("PDF object allowance exceeded")
        _check_deadline(deadline)
        pages = reader.pages
        if not 1 <= len(pages) <= settings.max_pdf_pages:
            raise ValueError("PDF page allowance exceeded")
        for page in pages:
            _check_deadline(deadline)
            scale = Decimal(str(page.get("/UserUnit", 1)))
            width = Decimal(str(page.mediabox.width)) * scale
            height = Decimal(str(page.mediabox.height)) * scale
            if any(
                not value.is_finite() or value <= 0 or value > settings.max_pdf_page_points
                for value in (scale, width, height)
            ):
                raise ValueError("PDF page geometry allowance exceeded")


def _check_image(document: RawDocument, settings: DocumentSettings, deadline: float) -> None:
    expected = "PNG" if document.content_type == "image/png" else "JPEG"
    with Image.open(BytesIO(document.body)) as image:
        width, height = image.size
        if (
            image.format != expected
            or getattr(image, "n_frames", 1) != 1
            or min(width, height) <= 0
            or max(width, height) > settings.max_image_dimension
            or width * height > settings.max_image_pixels
        ):
            raise ValueError("Image geometry or format allowance exceeded")
        image.verify()
    _check_deadline(deadline)
    with Image.open(BytesIO(document.body)) as image:
        image.load()


def _check_document(document: RawDocument, settings: DocumentSettings, deadline: float) -> None:
    if not document.body or len(document.body) > settings.max_document_bytes:
        raise ValueError("Document byte allowance exceeded")
    _check_deadline(deadline)
    if document.content_type == "application/pdf":
        _check_pdf(document.body, settings, deadline)
    else:
        _check_image(document, settings, deadline)
    _check_deadline(deadline)


class DocumentPreflight:
    def __init__(self, settings: DocumentSettings) -> None:
        self._settings = settings
        self._slots = asyncio.Semaphore(settings.max_parallel_parses)
        self._pending: set[asyncio.Task[None]] = set()

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._pending.discard(task)
        self._slots.release()
        if not task.cancelled():
            task.exception()  # Consume failures from a worker whose caller was already cancelled.

    async def check(self, document: RawDocument, *, run_id: UUID, trace_id: str) -> None:
        started = perf_counter()
        deadline = monotonic() + self._settings.preparation_timeout_seconds
        try:
            async with asyncio.timeout(self._settings.preparation_timeout_seconds):
                await self._slots.acquire()
                task = asyncio.create_task(
                    asyncio.to_thread(_check_document, document, self._settings, deadline)
                )
                self._pending.add(task)
                task.add_done_callback(self._finished)
                await asyncio.shield(task)
        except TimeoutError:
            logger.error("document_preflight_timeout run_id=%s trace_id=%s", run_id, trace_id)
            raise DocumentUnavailable(run_id=run_id, trace_id=trace_id) from None
        except (
            DecimalException,
            OverflowError,
            PyPdfError,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            SyntaxError,
            RecursionError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ):
            raise DocumentError(run_id=run_id, trace_id=trace_id) from None
        logger.info(
            "document_preflight_checked run_id=%s trace_id=%s version=%s bytes=%d duration_ms=%.3f",
            run_id,
            trace_id,
            PREFLIGHT_VERSION,
            len(document.body),
            (perf_counter() - started) * 1000,
        )

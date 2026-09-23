"""Raw storage identity, bounded decoding, cleanup, and cancellation without network I/O."""

import asyncio
from io import BytesIO
from threading import Event
from types import TracebackType
from typing import TYPE_CHECKING, Self, cast

import pytest
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import NameObject, TextStringObject
from tests.unit.extraction_support import (
    RUN_ID,
    TRACE_ID,
    extraction_request,
    pdf_bytes,
    raw_document,
)

from invoiceops_agent.schemas.documents import DocumentReference
from invoiceops_agent.tools.document_errors import DocumentError, DocumentUnavailable
from invoiceops_agent.tools.document_preflight import DocumentPreflight
from invoiceops_agent.tools.document_settings import DocumentSettings
from invoiceops_agent.tools.ingestion_schemas import RawDocument
from invoiceops_agent.tools.raw_document_reader import S3DocumentReader

if TYPE_CHECKING:
    from types_aiobotocore_s3.client import S3Client

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class Body:
    def __init__(self, data: bytes, *, fail: bool = False) -> None:
        self.data = data
        self.closed = False
        self.reads = 0
        self.fail = fail

    async def read(self, size: int = -1) -> bytes:
        self.reads += 1
        if self.fail:
            raise OSError("synthetic-private-storage-details")
        value, self.data = self.data[:size], self.data[size:]
        return value

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.closed = True


class FakeS3:
    def __init__(self, body: Body, **changes: object) -> None:
        self.result = {
            "Body": body,
            "ContentLength": len(body.data),
            "ContentType": "image/png",
            **changes,
        }
        self.calls: list[dict[str, object]] = []

    async def get_object(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return self.result


def reader(client: FakeS3, settings: DocumentSettings | None = None) -> S3DocumentReader:
    # The mock intentionally implements only the typed client's get_object operation.
    return S3DocumentReader(
        cast("S3Client", client), "invoiceops-raw", settings or DocumentSettings()
    )


async def test_raw_reader_verifies_and_closes_the_original_representation() -> None:
    document = raw_document()
    body = Body(document.body)
    client = FakeS3(body)
    result = await reader(client).read(extraction_request(), run_id=RUN_ID, trace_id=TRACE_ID)
    assert result.body == document.body and result.content_hash == document.content_hash
    assert body.closed
    assert client.calls == [{"Bucket": "invoiceops-raw", "Key": document.object_key}]


@pytest.mark.parametrize(
    "problem", ["bucket", "key", "type", "length", "truncated", "hash", "oversize", "read_failure"]
)
async def test_raw_reader_rejects_wrong_identity_or_payload_and_releases_stream(
    problem: str,
) -> None:
    document = raw_document()
    body = Body(document.body, fail=problem == "read_failure")
    ref: DocumentReference = extraction_request()
    changes: dict[str, object] = {}
    settings = DocumentSettings(max_document_bytes=100)
    if problem == "bucket":
        ref = ref.model_copy(
            update={"raw_ref": ref.raw_ref.replace("invoiceops-raw", "another-bucket")}
        )
    elif problem == "key":
        ref = ref.model_copy(update={"raw_ref": "s3://invoiceops-raw/other"})
    elif problem == "type":
        changes["ContentType"] = "application/pdf"
    elif problem == "length":
        changes["ContentLength"] = 1_000
    elif problem == "truncated":
        changes["ContentLength"] = len(document.body) + 1
    elif problem == "hash":
        body.data += b"x"
    elif problem == "oversize":
        body.data += b"x" * 100
        changes["ContentLength"] = len(document.body)
    client = FakeS3(body, **changes)
    with pytest.raises(DocumentError) as error:
        await reader(client, settings).read(ref, run_id=RUN_ID, trace_id=TRACE_ID)
    assert "synthetic-private-storage-details" not in str(error.value)
    if problem in {"bucket", "key"}:
        assert not client.calls
    else:
        assert body.closed


async def test_preflight_accepts_valid_images_and_bounded_pdf() -> None:
    preflight = DocumentPreflight(DocumentSettings())
    for document in (raw_document(), raw_document(pdf_bytes(), "application/pdf")):
        await preflight.check(document, run_id=RUN_ID, trace_id=TRACE_ID)


@pytest.mark.parametrize(
    "problem",
    [
        "dimension",
        "pixels",
        "truncated",
        "animated",
        "pdf_pages",
        "pdf_geometry",
        "pdf_objects",
        "encrypted",
        "user_unit",
    ],
)
async def test_preflight_rejects_malformed_or_unbounded_decoded_documents(problem: str) -> None:
    settings = DocumentSettings()
    document = raw_document()
    if problem == "dimension":
        settings = DocumentSettings(max_image_dimension=10)
    elif problem == "pixels":
        settings = DocumentSettings(max_image_pixels=100)
    elif problem == "truncated":
        document = raw_document(document.body[:10])
    elif problem == "animated":
        output = BytesIO()
        with (
            Image.new("RGB", (16, 16), "white") as first,
            Image.new("RGB", (16, 16), "black") as second,
        ):
            first.save(output, format="PNG", save_all=True, append_images=[second], duration=100)
        document = raw_document(output.getvalue())
    else:
        output = BytesIO()
        writer = PdfWriter()
        page = writer.add_blank_page(width=100, height=100)
        if problem == "pdf_pages":
            writer.add_blank_page(width=100, height=100)
            settings = DocumentSettings(max_pdf_pages=1)
        elif problem == "pdf_geometry":
            settings = DocumentSettings(max_pdf_page_points=50)
        elif problem == "pdf_objects":
            settings = DocumentSettings(max_pdf_objects=1)
        elif problem == "encrypted":
            writer.encrypt("synthetic-password")
        elif problem == "user_unit":
            page[NameObject("/UserUnit")] = TextStringObject("not-a-number")
        writer.write(output)
        writer.close()
        document = raw_document(output.getvalue(), "application/pdf")
    with pytest.raises(DocumentError):
        await DocumentPreflight(settings).check(document, run_id=RUN_ID, trace_id=TRACE_ID)


async def test_cancelled_parse_holds_its_slot_until_worker_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    release = Event()
    finished = asyncio.Event()
    loop = asyncio.get_running_loop()
    calls = 0

    def blocked(document: RawDocument, settings: DocumentSettings, deadline: float) -> None:
        nonlocal calls
        calls += 1
        loop.call_soon_threadsafe(entered.set)
        release.wait()
        loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr("invoiceops_agent.tools.document_preflight._check_document", blocked)
    preflight = DocumentPreflight(
        DocumentSettings(max_parallel_parses=1, preparation_timeout_seconds=0.02)
    )
    pending = asyncio.create_task(preflight.check(raw_document(), run_id=RUN_ID, trace_id=TRACE_ID))
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    try:
        with pytest.raises(DocumentUnavailable):
            await preflight.check(raw_document(), run_id=RUN_ID, trace_id=TRACE_ID)
        assert calls == 1
    finally:
        release.set()
        await finished.wait()

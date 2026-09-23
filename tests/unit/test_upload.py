"""Offline transport limits and authentication before reading any request bytes."""

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from io import BytesIO
from tempfile import SpooledTemporaryFile
from uuid import UUID

import pytest
from fastapi import FastAPI
from pydantic import SecretStr, ValidationError
from starlette.requests import ClientDisconnect
from tests.unit.test_api import assert_problem, client_for

from invoiceops_agent.api.app import create_app
from invoiceops_agent.api.settings import ApiSettings
from invoiceops_agent.graph.ingestion import UploadService
from invoiceops_agent.tools.documents import read_document
from invoiceops_agent.tools.ingestion_errors import IngestionUnavailable
from invoiceops_agent.tools.ingestion_schemas import IngestionOutcome, IngestionResult, RawDocument
from invoiceops_agent.tools.webhook_auth import WebhookNonce

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
TOKEN = "synthetic-upload-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": "upload-1"}
PDF = b"%PDF-1.7\nsynthetic-document\n%%EOF"


@dataclass
class CaptureUploads:
    documents: list[RawDocument] = field(default_factory=list)
    nonces: list[WebhookNonce | None] = field(default_factory=list)
    failure: Exception | None = None
    duplicate: bool = False

    async def ingest(
        self,
        document: RawDocument,
        *,
        key: str,
        trace_id: str,
        nonce: WebhookNonce | None = None,
    ) -> IngestionOutcome:
        self.documents.append(document)
        self.nonces.append(nonce)
        if self.failure is not None:
            raise self.failure
        return IngestionOutcome(
            response_status=200 if self.duplicate else 201,
            body=IngestionResult(
                invoice_id=UUID(int=1), run_id=UUID(int=2), duplicate=self.duplicate
            ),
        )


def make_app(service: CaptureUploads, settings: ApiSettings | None = None) -> FastAPI:

    @asynccontextmanager
    async def factory(configuration: ApiSettings) -> AsyncIterator[UploadService | None]:
        yield service

    return create_app(
        settings or ApiSettings(service_token=SecretStr(TOKEN)), upload_factory=factory
    )


def multipart(
    body: bytes = PDF, *, media_type: str = "application/pdf", name: str = "file"
) -> bytes:
    return (
        (
            f'--test-boundary\r\nContent-Disposition: form-data; name="{name}"; filename="test"\r\n'
            f"Content-Type: {media_type}\r\n\r\n"
        ).encode()
        + body
        + b"\r\n--test-boundary--\r\n"
    )


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic private"])
async def test_authentication_never_reads_body(authorization: str | None) -> None:
    read = False

    async def forbidden_body() -> AsyncIterator[bytes]:
        nonlocal read
        read = True
        raise AssertionError("Authentication must precede body reading")
        yield b""  # pragma: no cover

    headers = {"Idempotency-Key": "auth-check"}
    if authorization is not None:
        headers["Authorization"] = authorization
    service = CaptureUploads()
    async with client_for(make_app(service)) as client:
        response = await client.post("/v1/invoices", headers=headers, content=forbidden_body())
    assert_problem(response, 401)
    assert response.headers["www-authenticate"] == "Bearer"
    assert not read and not service.documents


@pytest.mark.parametrize(
    "media_type,body",
    [
        ("application/pdf", PDF),
        ("image/png", b"\x89PNG\r\n\x1a\nabc"),
        ("image/jpeg", b"\xff\xd8\xffabc"),
    ],
)
async def test_accepts_supported_signatures_and_hashes(media_type: str, body: bytes) -> None:
    service = CaptureUploads()
    async with client_for(make_app(service)) as client:
        response = await client.post(
            "/v1/invoices", headers=HEADERS, files={"file": ("ignored-name", body, media_type)}
        )
    assert response.status_code == 201
    assert response.json() == {
        "invoice_id": str(UUID(int=1)),
        "run_id": str(UUID(int=2)),
        "status": "QUEUED",
        "duplicate": False,
    }
    document = service.documents[0]
    assert document.body == body
    assert document.content_hash == hashlib.sha256(body).hexdigest()
    assert document.object_key == f"sha256/{document.content_hash[:2]}/{document.content_hash}"
    assert "synthetic-document" not in repr(document)


@pytest.mark.parametrize(
    "payload,content_type,status",
    [
        (multipart(b""), "multipart/form-data; boundary=test-boundary", 400),
        (multipart(b"not a PDF"), "multipart/form-data; boundary=test-boundary", 415),
        (
            multipart(PDF, media_type="text/plain"),
            "multipart/form-data; boundary=test-boundary",
            415,
        ),
        (multipart(name="other"), "multipart/form-data; boundary=test-boundary", 400),
        (multipart()[:-20], "multipart/form-data; boundary=test-boundary", 400),
        (multipart(), "multipart/form-data", 400),
        (b"{}", "application/json", 415),
    ],
)
async def test_invalid_documents_do_not_reach_storage(
    payload: bytes, content_type: str, status: int
) -> None:
    service = CaptureUploads()
    async with client_for(make_app(service)) as client:
        response = await client.post(
            "/v1/invoices", headers={**HEADERS, "Content-Type": content_type}, content=payload
        )
    assert_problem(response, status)
    assert not service.documents


async def test_extra_fields_or_files_rejected() -> None:
    service = CaptureUploads()
    async with client_for(make_app(service)) as client:
        for files, data in [
            (
                [("file", ("a", PDF, "application/pdf")), ("file", ("b", PDF, "application/pdf"))],
                {},
            ),
            ([("file", ("a", PDF, "application/pdf"))], {"unexpected": "value"}),
        ]:
            response = await client.post("/v1/invoices", headers=HEADERS, files=files, data=data)
            assert_problem(response, 400)
    assert not service.documents


async def test_spooled_document_hashes_across_chunks() -> None:
    body = PDF + b"synthetic" * (150 * 1024)
    service = CaptureUploads()
    async with client_for(make_app(service)) as client:
        response = await client.post(
            "/v1/invoices", headers=HEADERS, files={"file": ("a.pdf", body, "application/pdf")}
        )
    assert response.status_code == 201
    assert service.documents[0].content_hash == hashlib.sha256(body).hexdigest()


async def test_duplicate_auth_and_excessive_part_headers_are_rejected() -> None:
    service = CaptureUploads()
    async with client_for(make_app(service)) as client:
        response = await client.post(
            "/v1/invoices",
            headers=[*HEADERS.items(), ("Authorization", f"Bearer {TOKEN}")],
            files={"file": ("a.pdf", PDF, "application/pdf")},
        )
        assert_problem(response, 401)
        response = await client.post(
            "/v1/invoices",
            headers=HEADERS,
            files={"file": ("x" * 8193, PDF, "application/pdf")},
        )
        assert_problem(response, 400)
    assert not service.documents


@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize("document_limit,body_limit", [(8, 512), (128, 160)])
async def test_document_and_whole_body_limits(
    chunked: bool, document_limit: int, body_limit: int
) -> None:
    payload = multipart()

    async def chunks() -> AsyncIterator[bytes]:
        for start in range(0, len(payload), 7):
            yield payload[start : start + 7]

    service = CaptureUploads()
    settings = ApiSettings(
        service_token=SecretStr(TOKEN),
        document_max_bytes=document_limit,
        upload_max_bytes=body_limit,
    )
    async with client_for(make_app(service, settings)) as client:
        response = await client.post(
            "/v1/invoices",
            headers={**HEADERS, "Content-Type": "multipart/form-data; boundary=test-boundary"},
            content=chunks() if chunked else payload,
        )
    assert_problem(response, 413)
    assert not service.documents


async def test_bounded_stream_times_out_and_cancels() -> None:
    cancelled = asyncio.Event()

    async def slow_body() -> AsyncIterator[bytes]:
        try:
            yield multipart()[:-24]
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    service = CaptureUploads()
    settings = ApiSettings(service_token=SecretStr(TOKEN), upload_timeout_seconds=0.01)
    async with client_for(make_app(service, settings)) as client:
        response = await client.post(
            "/v1/invoices",
            headers={**HEADERS, "Content-Type": "multipart/form-data; boundary=test-boundary"},
            content=slow_body(),
        )
    assert_problem(response, 408)
    assert cancelled.is_set() and not service.documents


@pytest.mark.parametrize("ending", ["success", "truncate", "timeout", "disconnect", "oversize"])
async def test_rolled_upload_files_are_closed(ending: str, monkeypatch: pytest.MonkeyPatch) -> None:
    files: list[SpooledTemporaryFile[bytes]] = []

    def tracked_file(*, max_size: int) -> SpooledTemporaryFile[bytes]:
        spool: SpooledTemporaryFile[bytes] = SpooledTemporaryFile(max_size=max_size)
        files.append(spool)
        return spool

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", tracked_file)
    body = PDF + b"x" * (1200 * 1024)
    payload = multipart(body)

    async def chunks() -> AsyncIterator[bytes]:
        partial = payload if ending in {"success", "oversize"} else payload[:-24]
        for start in range(0, len(partial), 65536):
            yield partial[start : start + 65536]
        if ending == "timeout":
            await asyncio.Event().wait()
        if ending == "disconnect":
            raise ClientDisconnect()

    service = CaptureUploads()
    settings = ApiSettings(
        service_token=SecretStr(TOKEN),
        document_max_bytes=(1100 if ending == "oversize" else 1300) * 1024,
        upload_max_bytes=1400 * 1024,
        upload_timeout_seconds=1 if ending == "timeout" else 10,
    )
    async with client_for(make_app(service, settings)) as client:
        response = await client.post(
            "/v1/invoices",
            headers={**HEADERS, "Content-Type": "multipart/form-data; boundary=test-boundary"},
            content=chunks(),
        )
    assert (
        response.status_code
        == {
            "success": 201,
            "truncate": 400,
            "timeout": 408,
            "disconnect": 400,
            "oversize": 413,
        }[ending]
    )
    assert len(files) == 1 and files[0].closed
    assert getattr(files[0], "_rolled", False) is True


async def test_infrastructure_failure_uses_problem_details() -> None:
    service = CaptureUploads(failure=IngestionUnavailable("Invoice persistence is unavailable."))
    async with client_for(make_app(service)) as client:
        response = await client.post(
            "/v1/invoices", headers=HEADERS, files={"file": ("a", PDF, "application/pdf")}
        )
    assert_problem(response, 503)


async def test_upload_is_disabled_without_credentials() -> None:
    async with client_for(create_app()) as client:
        assert_problem(await client.post("/v1/invoices", headers=HEADERS), 503)


async def test_settings_reject_unsafe_bounds_and_short_tokens() -> None:
    with pytest.raises(ValidationError):
        ApiSettings(service_token=SecretStr("short"))
    with pytest.raises(ValidationError):
        ApiSettings(document_max_bytes=100, upload_max_bytes=100)


@pytest.mark.parametrize("character", ["\x00", "\x01", "\x1f", "\x7f", " ", "\t", "é"])
async def test_service_token_rejects_nonprintable_ascii(character: str) -> None:
    with pytest.raises(ValidationError):
        ApiSettings(service_token=SecretStr(TOKEN + character))


async def test_upload_openapi_exposes_auth_and_required_replay_header() -> None:
    schema = make_app(CaptureUploads()).openapi()
    operation = schema["paths"]["/v1/invoices"]["post"]
    assert operation["security"] == [{"ServiceToken": []}]
    assert schema["components"]["securitySchemes"]["ServiceToken"] == {
        "type": "http",
        "scheme": "bearer",
    }
    header = next(item for item in operation["parameters"] if item["name"] == "Idempotency-Key")
    assert header["in"] == "header" and header["required"] is True
    assert header["schema"]["maxLength"] == 128
    assert "multipart/form-data" in operation["requestBody"]["content"]
    assert "200" in operation["responses"] and "201" in operation["responses"]


async def test_duplicate_outcome_uses_200_body_contract() -> None:
    async with client_for(make_app(CaptureUploads(duplicate=True))) as client:
        response = await client.post(
            "/v1/invoices", headers=HEADERS, files={"file": ("a.pdf", PDF, "application/pdf")}
        )
    assert response.status_code == 200
    assert response.json() == {
        "invoice_id": str(UUID(int=1)),
        "run_id": str(UUID(int=2)),
        "status": "QUEUED",
        "duplicate": True,
    }


@pytest.mark.parametrize("status,duplicate", [(200, False), (201, True), (202, False)])
async def test_inconsistent_persisted_outcomes_fail_validation(
    status: int, duplicate: bool
) -> None:
    with pytest.raises(ValidationError):
        IngestionOutcome.model_validate(
            {
                "response_status": status,
                "body": {"invoice_id": UUID(int=1), "run_id": UUID(int=2), "duplicate": duplicate},
            }
        )


async def test_source_changes_replay_identity_but_not_content_identity() -> None:
    class Document:
        def __init__(self) -> None:
            self.buffer = BytesIO(PDF)

        async def read(self, size: int = -1) -> bytes:
            return self.buffer.read(size)

    upload = await read_document(Document(), "application/pdf", max_bytes=1024)
    email = await read_document(Document(), "application/pdf", max_bytes=1024, source="EMAIL")
    assert upload.content_hash == email.content_hash
    assert upload.request_hash != email.request_hash
    assert upload.source == "UPLOAD" and email.source == "EMAIL"

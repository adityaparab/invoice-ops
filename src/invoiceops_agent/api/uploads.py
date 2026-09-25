"""Authenticated, bounded multipart transport; document rules live in deterministic tools."""

import asyncio
import hmac
from collections.abc import AsyncGenerator

from fastapi import HTTPException
from python_multipart.multipart import parse_options_header
from starlette.datastructures import Headers, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect, Request

from invoiceops_agent.api.auth_store import bearer_token
from invoiceops_agent.api.body import bounded_body_stream, validate_content_length
from invoiceops_agent.api.settings import ApiSettings
from invoiceops_agent.tools.documents import read_document
from invoiceops_agent.tools.ingestion_errors import DocumentTooLarge, InvalidDocument
from invoiceops_agent.tools.ingestion_schemas import RawDocument


async def authenticate_upload(request: Request, settings: ApiSettings) -> None:
    candidate = bearer_token(request.headers.getlist("Authorization"))
    if candidate is not None and candidate.startswith("io_"):
        user = await request.app.state.auth_store.resolve(candidate)
        if user is None:
            raise HTTPException(401, "Login session is invalid or expired.")
        if user.role != "ANALYST":
            raise HTTPException(403, "Only the analyst can upload invoices.")
        return
    if settings.service_token is None:
        raise HTTPException(503, "Invoice uploads are not configured.")
    supplied = request.headers.getlist("Authorization")
    expected = f"Bearer {settings.service_token.get_secret_value()}".encode("ascii")
    if len(supplied) != 1 or not hmac.compare_digest(supplied[0].encode("utf-8"), expected):
        raise HTTPException(
            401, "A valid service token is required.", headers={"WWW-Authenticate": "Bearer"}
        )


class BoundedMultipartParser(MultiPartParser):
    """Starlette spools files; callback bounds include file bytes and multipart headers."""

    def __init__(
        self, headers: Headers, stream: AsyncGenerator[bytes, None], document_limit: int
    ) -> None:
        super().__init__(headers, stream, max_files=1, max_fields=0)
        self.document_limit = document_limit
        self.part_bytes = 0
        self.header_bytes = 0
        self.complete = False
        self.created_uploads: list[UploadFile] = []

    def on_part_begin(self) -> None:
        super().on_part_begin()
        self.part_bytes = 0
        self.header_bytes = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        self.part_bytes += end - start
        if self.part_bytes > self.document_limit:
            raise DocumentTooLarge("Document exceeds the configured size limit.")
        super().on_part_data(data, start, end)

    def _count_header(self, size: int) -> None:
        self.header_bytes += size
        if self.header_bytes > 8192:
            raise InvalidDocument("Multipart headers exceed the supported limit.")

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._count_header(end - start)
        super().on_header_field(data, start, end)

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._count_header(end - start)
        super().on_header_value(data, start, end)

    def on_end(self) -> None:
        self.complete = True

    def on_headers_finished(self) -> None:
        super().on_headers_finished()
        # A truncated part may never reach FormData; still retain it for async cleanup.
        if self._current_part.file is not None:
            self.created_uploads.append(self._current_part.file)

    async def close(self) -> None:
        for upload in self.created_uploads:
            await upload.close()


async def parse_upload(request: Request, settings: ApiSettings) -> RawDocument:
    declared_types = request.headers.getlist("Content-Type")
    if len(declared_types) != 1:
        raise HTTPException(415, "Use multipart/form-data with exactly one file part.")
    media_type, params = parse_options_header(declared_types[0])
    if media_type != b"multipart/form-data":
        raise HTTPException(415, "Use multipart/form-data with exactly one file part.")
    boundary = params.get(b"boundary", b"")
    if not boundary or len(boundary) > 200:
        raise InvalidDocument("A valid multipart boundary is required.")
    validate_content_length(request, settings.upload_max_bytes)
    parser = BoundedMultipartParser(
        request.headers,
        bounded_body_stream(request, settings.upload_max_bytes),
        settings.document_max_bytes,
    )
    try:
        async with asyncio.timeout(settings.upload_timeout_seconds):
            form = await parser.parse()
            try:
                parts = form.multi_items()
                if not parser.complete or len(parts) != 1 or parts[0][0] != "file":
                    raise InvalidDocument("Supply exactly one complete multipart file named file.")
                upload = parts[0][1]
                if not isinstance(upload, UploadFile):
                    raise InvalidDocument("The file part must be a document upload.")
                if len(upload.headers.getlist("Content-Type")) != 1:
                    raise InvalidDocument("The document must declare exactly one media type.")
                return await read_document(
                    upload, upload.content_type, max_bytes=settings.document_max_bytes
                )
            finally:
                await form.close()
    except (MultiPartException, ClientDisconnect) as error:
        raise InvalidDocument("The multipart upload is invalid or incomplete.") from error
    except TimeoutError as error:
        raise HTTPException(408, "The upload exceeded its time limit.") from error
    finally:
        await parser.close()

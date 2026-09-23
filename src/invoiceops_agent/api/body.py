"""Shared actual-byte accounting for bounded multipart and signed JSON bodies."""

from collections.abc import AsyncGenerator

from starlette.requests import Request

from invoiceops_agent.tools.ingestion_errors import DocumentTooLarge, InvalidDocument


def validate_content_length(request: Request, max_bytes: int) -> None:
    lengths = request.headers.getlist("Content-Length")
    if lengths:
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            raise InvalidDocument("A valid Content-Length is required when supplied.")
        if len(lengths[0]) > 12 or int(lengths[0]) > max_bytes:
            raise DocumentTooLarge("Request exceeds the configured whole-body size limit.")


async def bounded_body_stream(request: Request, max_bytes: int) -> AsyncGenerator[bytes, None]:
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > max_bytes:
            raise DocumentTooLarge("Request exceeds the configured whole-body size limit.")
        yield chunk

"""Bounded raw-body authentication before parsing the signed JSON envelope."""

import asyncio
import base64
import binascii
import io
import json
from collections.abc import Callable
from datetime import datetime

from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import ClientDisconnect, Request

from invoiceops_agent.api.body import bounded_body_stream, validate_content_length
from invoiceops_agent.api.schemas.email import EmailWebhookRequest
from invoiceops_agent.api.settings import ApiSettings
from invoiceops_agent.tools.documents import read_document
from invoiceops_agent.tools.ingestion_errors import DocumentTooLarge, InvalidDocument
from invoiceops_agent.tools.ingestion_schemas import RawDocument
from invoiceops_agent.tools.webhook_auth import (
    WebhookAuthenticationError,
    WebhookNonce,
    authenticate_signature,
    parse_signature_headers,
)


def _one_header(request: Request, name: str) -> str:
    values = request.headers.getlist(name)
    if len(values) != 1:
        raise WebhookAuthenticationError("Invalid webhook authentication.")
    return values[0]


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidDocument("Duplicate JSON fields are not permitted.")
        result[key] = value
    return result


class _MemoryDocument:
    def __init__(self, body: bytes) -> None:
        self._stream = io.BytesIO(body)

    async def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


async def decode_email_document(envelope: EmailWebhookRequest, *, max_bytes: int) -> RawDocument:
    encoded = envelope.attachment.content_base64
    if len(encoded) > 4 * ((max_bytes + 2) // 3):
        raise DocumentTooLarge("Document exceeds the configured size limit.")
    try:
        body = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as error:
        raise InvalidDocument("The attachment must use valid base64.") from error
    if base64.b64encode(body).decode("ascii") != encoded:
        raise InvalidDocument("The attachment must use canonical base64.")
    return await read_document(
        _MemoryDocument(body),
        envelope.attachment.content_type,
        max_bytes=max_bytes,
        source="EMAIL",
    )


async def read_signed_email(
    request: Request, settings: ApiSettings, *, clock: Callable[[], datetime]
) -> tuple[EmailWebhookRequest, WebhookNonce]:
    if settings.webhook_secret is None:
        raise HTTPException(503, "Email webhooks are not configured.")
    try:
        signed = parse_signature_headers(
            _one_header(request, "X-Webhook-Timestamp"),
            _one_header(request, "X-Webhook-Nonce"),
            _one_header(request, "X-Webhook-Signature"),
            now=clock(),
            window_seconds=settings.webhook_window_seconds,
        )
        content_types = request.headers.getlist("Content-Type")
        if (
            len(content_types) != 1
            or content_types[0].split(";", 1)[0].strip().lower() != "application/json"
        ):
            raise HTTPException(415, "Use application/json for the signed email attachment.")
        validate_content_length(request, settings.webhook_max_bytes)
        async with asyncio.timeout(settings.webhook_timeout_seconds):
            raw = bytearray()
            async for chunk in bounded_body_stream(request, settings.webhook_max_bytes):
                raw.extend(chunk)
            nonce = authenticate_signature(
                signed, bytes(raw), secret=settings.webhook_secret.get_secret_value()
            )
            try:
                payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
            except (ValueError, RecursionError) as error:
                raise InvalidDocument("The webhook body must contain valid JSON.") from error
            envelope = EmailWebhookRequest.model_validate(payload)
        return envelope, nonce
    except WebhookAuthenticationError as error:
        raise HTTPException(401, "Invalid webhook authentication.") from error
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ClientDisconnect) as error:
        raise InvalidDocument("The webhook body must match the attachment schema.") from error
    except TimeoutError as error:
        raise HTTPException(408, "The webhook exceeded its receive time limit.") from error

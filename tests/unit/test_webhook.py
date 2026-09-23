"""Offline signed-body authentication and bounded JSON transport checks."""

import asyncio
import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError
from starlette.requests import Request
from starlette.types import Message, Scope

from invoiceops_agent.api.schemas.email import EmailWebhookRequest, email_request_schema
from invoiceops_agent.api.settings import ApiSettings
from invoiceops_agent.api.webhooks import read_signed_email
from invoiceops_agent.tools.ingestion_errors import DocumentTooLarge, InvalidDocument
from invoiceops_agent.tools.webhook_auth import (
    WebhookAuthenticationError,
    authenticate_signature,
    parse_signature_headers,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
SECRET = "synthetic-email-webhook-signing-secret"
NOW = datetime(2026, 9, 23, 12, tzinfo=UTC)
TIMESTAMP = int(NOW.timestamp())
NONCE = "synthetic-nonce-0001"
PDF = b"%PDF-1.7\nsynthetic email invoice\n%%EOF"


def envelope(body: bytes = PDF, *, content_type: str = "application/pdf") -> bytes:
    return json.dumps(
        {
            "attachment": {
                "content_type": content_type,
                "content_base64": base64.b64encode(body).decode("ascii"),
            }
        }
    ).encode("utf-8")


def signed_headers(
    body: bytes, *, nonce: str = NONCE, timestamp: int = TIMESTAMP, key: str = "email-request-1"
) -> dict[str, str]:
    signature = hmac.new(
        SECRET.encode(), f"{timestamp}.{nonce}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "Idempotency-Key": key,
        "X-Webhook-Timestamp": str(timestamp),
        "X-Webhook-Nonce": nonce,
        "X-Webhook-Signature": signature,
    }


def request_for(body: bytes, headers: list[tuple[str, str]], *, chunk_size: int = 65536) -> Request:
    chunks = iter(body[start : start + chunk_size] for start in range(0, len(body), chunk_size))

    async def receive() -> Message:
        chunk = next(chunks, b"")
        return {"type": "http.request", "body": chunk, "more_body": bool(chunk)}

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/invoices/email-webhook",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers],
    }
    return Request(scope, receive)


async def test_signature_validates_exact_raw_bytes_and_injected_receipt_time() -> None:
    raw = envelope()
    request = request_for(raw, list(signed_headers(raw).items()), chunk_size=7)
    model, nonce = await read_signed_email(
        request, ApiSettings(webhook_secret=SecretStr(SECRET)), clock=lambda: NOW
    )
    assert model.attachment.content_base64 == base64.b64encode(PDF).decode()
    assert nonce.nonce == NONCE and int(nonce.signed_at.timestamp()) == TIMESTAMP
    assert model.attachment.content_base64 not in repr(model)


@pytest.mark.parametrize("offset", [-301, -300, 300, 301])
async def test_timestamp_window_inclusive_boundaries(offset: int) -> None:
    raw = envelope()
    headers = signed_headers(raw, timestamp=TIMESTAMP + offset)
    request = request_for(raw, list(headers.items()))
    if abs(offset) <= 300:
        await read_signed_email(
            request, ApiSettings(webhook_secret=SecretStr(SECRET)), clock=lambda: NOW
        )
    else:
        with pytest.raises(HTTPException) as error:
            await read_signed_email(
                request, ApiSettings(webhook_secret=SecretStr(SECRET)), clock=lambda: NOW
            )
        assert error.value.status_code == 401


@pytest.mark.parametrize(
    "header", ["X-Webhook-Timestamp", "X-Webhook-Nonce", "X-Webhook-Signature"]
)
async def test_duplicate_auth_headers_reject_before_body_read(header: str) -> None:
    raw = envelope()
    headers = signed_headers(raw)
    request = request_for(raw, [*headers.items(), (header, headers[header])])

    async def forbidden_receive() -> Message:
        raise AssertionError("Malformed authentication must not read the body")

    request = Request(request.scope, forbidden_receive)
    with pytest.raises(HTTPException) as error:
        await read_signed_email(
            request, ApiSettings(webhook_secret=SecretStr(SECRET)), clock=lambda: NOW
        )
    assert error.value.status_code == 401


@pytest.mark.parametrize(
    "field,value",
    [
        ("X-Webhook-Timestamp", "not-a-time"),
        ("X-Webhook-Timestamp", "999999999999"),
        ("X-Webhook-Timestamp", "01758595200"),
        ("X-Webhook-Nonce", "short"),
        ("X-Webhook-Nonce", "n" * 129),
        ("X-Webhook-Nonce", "nonce with invalid spaces"),
        ("X-Webhook-Signature", "invalid"),
    ],
)
async def test_malformed_authentication(field: str, value: str) -> None:
    raw = envelope()
    headers = {**signed_headers(raw), field: value}
    with pytest.raises(HTTPException) as error:
        await read_signed_email(
            request_for(raw, list(headers.items())),
            ApiSettings(webhook_secret=SecretStr(SECRET)),
            clock=lambda: NOW,
        )
    assert error.value.status_code == 401


async def test_signature_failure_precedes_json_parsing() -> None:
    raw = b"{malformed-private-document"
    headers = {**signed_headers(raw), "X-Webhook-Signature": "0" * 64}
    with pytest.raises(HTTPException) as error:
        await read_signed_email(
            request_for(raw, list(headers.items())),
            ApiSettings(webhook_secret=SecretStr(SECRET)),
            clock=lambda: NOW,
        )
    assert error.value.status_code == 401
    assert "private-document" not in str(error.value)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not-json",
        b"\xff",
        b"[]",
        b"{}",
        b'{"attachment":{},"attachment":{}}',
        b'{"attachment":{"content_type":"application/pdf","content_base64":"YQ=="},"from":"private"}',
    ],
)
async def test_authenticated_invalid_envelope_is_rejected(raw: bytes) -> None:
    with pytest.raises(InvalidDocument):
        await read_signed_email(
            request_for(raw, list(signed_headers(raw).items())),
            ApiSettings(webhook_secret=SecretStr(SECRET)),
            clock=lambda: NOW,
        )


@pytest.mark.parametrize("declared_length", [False, True])
async def test_raw_body_limits_cover_chunked_input(declared_length: bool) -> None:
    raw = envelope()
    headers = signed_headers(raw)
    if declared_length:
        headers["Content-Length"] = str(len(raw))
    with pytest.raises(DocumentTooLarge):
        await read_signed_email(
            request_for(raw, list(headers.items()), chunk_size=7),
            ApiSettings(webhook_secret=SecretStr(SECRET), webhook_max_bytes=len(raw) - 1),
            clock=lambda: NOW,
        )


async def test_receive_deadline_cancels_stuck_request() -> None:
    raw = envelope()
    template = request_for(raw, list(signed_headers(raw).items()))
    cancelled = asyncio.Event()

    async def blocked_receive() -> Message:
        try:
            await asyncio.Event().wait()
            return {"type": "http.disconnect"}
        finally:
            cancelled.set()

    with pytest.raises(HTTPException) as error:
        await read_signed_email(
            Request(template.scope, blocked_receive),
            ApiSettings(webhook_secret=SecretStr(SECRET), webhook_timeout_seconds=0.01),
            clock=lambda: NOW,
        )
    assert error.value.status_code == 408 and cancelled.is_set()


async def test_hmac_covers_nonce_timestamp_and_body() -> None:
    raw = envelope()
    headers = signed_headers(raw)
    signed = parse_signature_headers(
        str(TIMESTAMP), NONCE, headers["X-Webhook-Signature"], now=NOW, window_seconds=300
    )
    with pytest.raises(WebhookAuthenticationError):
        authenticate_signature(signed, raw + b" ", secret=SECRET)
    with pytest.raises(WebhookAuthenticationError):
        authenticate_signature(signed, raw, secret="wrong-secret")


async def test_schema_and_secret_configuration() -> None:
    with pytest.raises(ValidationError):
        ApiSettings(webhook_secret=SecretStr("short"))
    with pytest.raises(ValidationError):
        ApiSettings(webhook_secret=SecretStr(SECRET + "\x00"))
    schema = email_request_schema()
    assert "$defs" not in schema and schema["additionalProperties"] is False
    assert (
        EmailWebhookRequest.model_validate_json(envelope()).attachment.content_type
        == "application/pdf"
    )

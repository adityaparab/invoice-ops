"""Deterministic HMAC verification with an injected receipt time and no external I/O."""

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

NONCE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$"
TIMESTAMP_PATTERN = r"^(0|[1-9][0-9]{0,11})$"
SIGNATURE_PATTERN = r"^[0-9a-f]{64}$"


class WebhookAuthenticationError(Exception):
    """Untrusted signature metadata or message authentication failed."""


@dataclass(frozen=True)
class WebhookNonce:
    nonce: str
    signed_at: datetime


@dataclass(frozen=True)
class WebhookSignature:
    timestamp: str
    nonce: WebhookNonce
    signature: str = field(repr=False)


def parse_signature_headers(
    timestamp: str, nonce: str, signature: str, *, now: datetime, window_seconds: int
) -> WebhookSignature:
    if not (
        re.fullmatch(TIMESTAMP_PATTERN, timestamp)
        and re.fullmatch(NONCE_PATTERN, nonce)
        and re.fullmatch(SIGNATURE_PATTERN, signature)
    ):
        raise WebhookAuthenticationError("Invalid webhook authentication.")
    if now.utcoffset() is None:
        raise ValueError("Webhook clock must be timezone-aware")
    try:
        signed_at = datetime.fromtimestamp(int(timestamp), UTC)
    except (ValueError, OverflowError, OSError) as error:
        raise WebhookAuthenticationError("Invalid webhook authentication.") from error
    if abs((now - signed_at).total_seconds()) > window_seconds:
        raise WebhookAuthenticationError("Invalid webhook authentication.")
    return WebhookSignature(timestamp, WebhookNonce(nonce, signed_at), signature)


def authenticate_signature(signed: WebhookSignature, body: bytes, *, secret: str) -> WebhookNonce:
    digest = hmac.new(secret.encode("utf-8"), digestmod=hashlib.sha256)
    digest.update(f"{signed.timestamp}.{signed.nonce.nonce}.".encode("ascii"))
    digest.update(body)
    if not hmac.compare_digest(digest.hexdigest(), signed.signature):
        raise WebhookAuthenticationError("Invalid webhook authentication.")
    return signed.nonce

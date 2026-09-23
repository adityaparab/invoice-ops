"""Retry classification and server hints, independent of transport and sleeping."""

import math
from datetime import datetime
from email.utils import parsedate_to_datetime

from openai import APIConnectionError, APIStatusError

_NONRETRYABLE_CODES = frozenset(
    {
        "insufficient_quota",
        "billing_hard_limit_reached",
        "billing_not_active",
        "account_deactivated",
        "invalid_api_key",
        "quota_exceeded",
        "budget_exceeded",
        "spend_limit_exceeded",
        "model_not_found",
        "context_length_exceeded",
    }
)


def is_retryable(error: APIConnectionError | APIStatusError) -> bool:
    if isinstance(error, APIConnectionError):
        return True
    body = error.body
    if isinstance(body, dict):
        nested = body.get("error", body)
        if isinstance(nested, dict) and any(
            nested.get(field) in _NONRETRYABLE_CODES
            for field in ("code", "type")
            if isinstance(nested.get(field), str)
        ):
            return False
    return error.status_code in {408, 429} or 500 <= error.status_code < 600


def retry_after(error: APIStatusError, now: datetime) -> float | None:
    value = error.response.headers.get("retry-after")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                return None
            seconds = (deadline - now).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds

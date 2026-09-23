"""Shared exact-decimal and fingerprint contracts for deterministic decisions."""

import hashlib
import json
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, BeforeValidator

from invoiceops_agent.schemas.extraction import DecimalJson


def reject_float(value: object) -> object:
    if isinstance(value, (float, bool)):
        raise ValueError("Decimal values must not originate from floats or booleans")
    return value


StrictDecimal = Annotated[Decimal, BeforeValidator(reject_float)]
ExactDecimal = Annotated[Decimal, BeforeValidator(reject_float), DecimalJson]


def model_digest(model: BaseModel) -> str:
    """Fingerprint the exact JSON contract without ambient Decimal arithmetic."""
    encoded = json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

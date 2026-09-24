"""Versioned, deterministic retry classification for invoice workflow infrastructure."""

from decimal import Decimal
from typing import Literal, Self

import psycopg
from pydantic import BaseModel, ConfigDict, Field, model_validator

from invoiceops_agent.gateway_client.errors import GatewayUnavailable
from invoiceops_agent.graph.errors import CheckpointUnavailable, GraphTimeout
from invoiceops_agent.ledger.errors import LedgerStorageError
from invoiceops_agent.tools.document_errors import DocumentUnavailable


class RetryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    version: Literal["invoice-retry@v2"] = "invoice-retry@v2"
    max_attempts: int = Field(default=3, ge=1, le=10)
    initial_delay_seconds: Decimal = Field(default=Decimal("0.5"), ge=0, le=60)
    max_delay_seconds: Decimal = Field(default=Decimal("5"), ge=0, le=300)
    running_lease_seconds: int = Field(default=480, ge=1, le=3600)

    @model_validator(mode="after")
    def ordered_delays(self) -> Self:
        if self.max_delay_seconds < self.initial_delay_seconds:
            raise ValueError("Maximum retry delay must be at least the initial delay")
        return self

    def delay(self, failed_attempt: int) -> float:
        if failed_attempt < 1:
            raise ValueError("Attempt numbers start at one")
        return float(
            min(self.initial_delay_seconds * 2 ** (failed_attempt - 1), self.max_delay_seconds)
        )


_INFRASTRUCTURE = (
    GraphTimeout,
    CheckpointUnavailable,
    LedgerStorageError,
    GatewayUnavailable,
    DocumentUnavailable,
    psycopg.OperationalError,
    psycopg.InterfaceError,
    psycopg.errors.DeadlockDetected,
    psycopg.errors.SerializationFailure,
    TimeoutError,
    ConnectionError,
    OSError,
)


def root_error(error: BaseException) -> BaseException:
    """Unwrap sanitized graph errors without exposing their messages."""
    current = error
    seen: set[int] = set()
    while current.__cause__ is not None and id(current) not in seen:
        seen.add(id(current))
        current = current.__cause__
    return current


def is_infrastructure_error(error: BaseException) -> bool:
    return isinstance(root_error(error), _INFRASTRUCTURE)

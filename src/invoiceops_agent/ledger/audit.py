"""Commit one audit event in a short transaction after external work has completed."""

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from time import perf_counter
from typing import Protocol

import psycopg
from psycopg.pq import TransactionStatus
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from invoiceops_agent.ledger.connection import LedgerConnection
from invoiceops_agent.ledger.errors import LedgerStorageError, LedgerTransactionRequired
from invoiceops_agent.ledger.schemas import AppendEvent, LedgerEvent

logger = logging.getLogger(__name__)


class AuditWriter(Protocol):
    async def append(
        self, connection: LedgerConnection, command: AppendEvent, *, trace_id: str
    ) -> LedgerEvent: ...


class AuditSink(Protocol):
    async def append(self, command: AppendEvent, *, trace_id: str) -> LedgerEvent:
        """Return only after the event has committed; propagate failures and cancellation."""
        ...


class TransactionalConnection(LedgerConnection, Protocol):
    def transaction(self) -> AbstractAsyncContextManager[object]: ...


class AuditSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INVOICEOPS_AUDIT_", extra="ignore", hide_input_in_errors=True
    )
    timeout_seconds: float = Field(default=10, gt=0, le=60, allow_inf_nan=False)


class TransactionalAuditSink:
    def __init__(
        self,
        connection: Callable[[], AbstractAsyncContextManager[TransactionalConnection]],
        writer: AuditWriter,
        settings: AuditSettings | None = None,
    ) -> None:
        self._connection = connection
        self._writer = writer
        self._settings = settings if settings is not None else AuditSettings()

    async def append(self, command: AppendEvent, *, trace_id: str) -> LedgerEvent:
        started = perf_counter()
        try:
            async with (
                asyncio.timeout(self._settings.timeout_seconds),
                self._connection() as connection,
            ):
                if connection.info.transaction_status != TransactionStatus.IDLE:
                    raise LedgerTransactionRequired(
                        "Audit sink requires an idle connection for its owned transaction.",
                        run_id=command.run_id,
                        trace_id=trace_id,
                    )
                async with connection.transaction():
                    event = await self._writer.append(connection, command, trace_id=trace_id)
        except (psycopg.Error, TimeoutError, OSError):
            logger.error("audit_commit_failed run_id=%s trace_id=%s", command.run_id, trace_id)
            raise LedgerStorageError(
                "Audit event could not be committed.", run_id=command.run_id, trace_id=trace_id
            ) from None
        logger.info(
            "audit_committed run_id=%s invoice_id=%s trace_id=%s event_id=%s duration_ms=%.3f",
            command.run_id,
            command.invoice_id,
            trace_id,
            event.id,
            (perf_counter() - started) * 1000,
        )
        return event

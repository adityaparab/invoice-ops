"""Append-only events staged inside the caller's transaction, never independently committed."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from time import perf_counter
from uuid import UUID, uuid4

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from invoiceops_agent.ledger.connection import LedgerConnection
from invoiceops_agent.ledger.errors import (
    LedgerConflict,
    LedgerError,
    LedgerIdentityMismatch,
    LedgerRunNotFound,
    LedgerStorageError,
    LedgerTransactionRequired,
)
from invoiceops_agent.ledger.schemas import (
    AppendEvent,
    EventIdentity,
    LedgerContext,
    LedgerEvent,
    NextSequence,
    RunIdentity,
)
from invoiceops_agent.ledger.settings import LedgerSettings

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


class LedgerWriter:
    def __init__(
        self,
        settings: LedgerSettings,
        *,
        clock: Callable[[], datetime] = utc_now,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self.new_id = new_id

    async def append(
        self, connection: LedgerConnection, command: AppendEvent, *, trace_id: str
    ) -> LedgerEvent:
        """Stage an event atomically with business writes; caller owns commit or rollback."""
        LedgerContext(trace_id=trace_id)
        if connection.info.transaction_status is not TransactionStatus.INTRANS:
            raise LedgerTransactionRequired(
                "Append requires an active caller-owned transaction",
                run_id=command.run_id,
                trace_id=trace_id,
            )
        started = perf_counter()
        try:
            event = await self._append(connection, command, trace_id)
        except LedgerError as error:
            logger.warning(
                "ledger_append_rejected run_id=%s trace_id=%s error_type=%s",
                command.run_id,
                trace_id,
                type(error).__name__,
            )
            raise
        except psycopg.IntegrityError as error:
            logger.warning(
                "ledger_append_conflict run_id=%s trace_id=%s error_type=%s",
                command.run_id,
                trace_id,
                type(error).__name__,
            )
            raise LedgerConflict(
                "Ledger append conflicts with persisted constraints",
                run_id=command.run_id,
                trace_id=trace_id,
            ) from error
        except (psycopg.Error, ValidationError) as error:
            logger.error(
                "ledger_append_failed run_id=%s trace_id=%s error_type=%s",
                command.run_id,
                trace_id,
                type(error).__name__,
            )
            raise LedgerStorageError(
                "Ledger append failed", run_id=command.run_id, trace_id=trace_id
            ) from error
        logger.info(
            "ledger_append_staged run_id=%s invoice_id=%s trace_id=%s "
            "event_id=%s sequence=%d duration_ms=%.3f",
            event.run_id,
            event.invoice_id,
            trace_id,
            event.id,
            event.sequence,
            (perf_counter() - started) * 1000,
        )
        return event

    async def _append(
        self, connection: LedgerConnection, command: AppendEvent, trace_id: str
    ) -> LedgerEvent:
        cursor = await connection.execute(
            "SELECT invoice_id, graph_version, trace_id FROM public.runs WHERE id = %s FOR UPDATE",
            (command.run_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise LedgerRunNotFound("Run does not exist", run_id=command.run_id, trace_id=trace_id)
        identity = RunIdentity.model_validate(row)
        versions = self.settings.resolve(command.versions)
        if (
            identity.invoice_id != command.invoice_id
            or identity.graph_version != versions.graph_version
        ):
            raise LedgerIdentityMismatch(
                "Event invoice or graph version does not match its run",
                run_id=command.run_id,
                trace_id=trace_id,
            )
        if command.supersedes_id is not None:
            await self._check_supersession(connection, command, trace_id)
        cursor = await connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence "
            "FROM public.ledger WHERE run_id = %s",
            (command.run_id,),
        )
        sequence = NextSequence.model_validate(await cursor.fetchone()).sequence
        event = LedgerEvent(
            **command.model_dump(exclude={"versions"}),
            id=self.new_id(),
            sequence=sequence,
            versions=versions,
            created_at=self.clock(),
        )
        await connection.execute(
            "INSERT INTO public.ledger (id, run_id, invoice_id, sequence, event_type, node, "
            "actor_type, actor_id, graph_version, model_version, prompt_version, policy_version, "
            "payload, supersedes_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                event.id,
                event.run_id,
                event.invoice_id,
                event.sequence,
                event.event_type,
                event.node,
                event.actor_type,
                event.actor_id,
                event.versions.graph_version,
                event.versions.model_version,
                event.versions.prompt_version,
                event.versions.policy_version,
                Jsonb(event.payload),
                event.supersedes_id,
                event.created_at,
            ),
        )
        return event

    @staticmethod
    async def _check_supersession(
        connection: LedgerConnection, command: AppendEvent, trace_id: str
    ) -> None:
        cursor = await connection.execute(
            "SELECT run_id, invoice_id FROM public.ledger WHERE id = %s", (command.supersedes_id,)
        )
        row = await cursor.fetchone()
        previous = EventIdentity.model_validate(row) if row is not None else None
        if (
            previous is None
            or previous.run_id != command.run_id
            or previous.invoice_id != command.invoice_id
        ):
            raise LedgerIdentityMismatch(
                "Superseded event must belong to this invoice and run",
                run_id=command.run_id,
                trace_id=trace_id,
            )

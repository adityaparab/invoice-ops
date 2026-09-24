"""Durable invoice attempts, bounded infrastructure retries, and run-backed dead letters."""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

import psycopg
from psycopg.rows import DictRow
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from invoiceops_agent.graph.errors import RunInProgress, RunNotFound
from invoiceops_agent.graph.retry import RetryConfig, is_infrastructure_error, root_error
from invoiceops_agent.graph.state import InvoiceGraphState
from invoiceops_agent.ledger.schemas import AppendEvent, VersionOverrides
from invoiceops_agent.ledger.settings import LedgerSettings
from invoiceops_agent.ledger.writer import LedgerWriter

logger = logging.getLogger(__name__)
type ConnectionFactory = Callable[[], AbstractAsyncContextManager[psycopg.AsyncConnection[DictRow]]]
type RunOnce = Callable[[UUID], Awaitable[InvoiceGraphState]]
type Clock = Callable[[], datetime]
type Sleep = Callable[[float], Awaitable[None]]
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ACTOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")


class WorkerError(Exception):
    """The run cannot enter the retry worker in its current state."""


class RunInDeadLetter(WorkerError):
    """The run needs an explicit idempotent operator redrive."""


class InvalidRedrive(WorkerError):
    """The requested redrive is invalid or conflicts with an earlier operation."""


class RetryState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    attempts: int = Field(default=0, ge=0, le=10)
    cycle: int = Field(default=0, ge=0)
    error_type: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
    retryable: bool | None = None
    dead_lettered: bool = False


class RunAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    invoice_id: UUID
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    graph_version: str = Field(min_length=1, max_length=128)
    attempt: int = Field(ge=1, le=10)
    cycle: int = Field(ge=0)


class DeadLetter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    invoice_id: UUID
    attempts: int = Field(ge=1)
    cycle: int = Field(ge=0)
    error_type: str
    retryable: bool


def utc_now() -> datetime:
    return datetime.now(UTC)


def _state(value: object) -> RetryState:
    if value is None:
        return RetryState()
    try:
        return RetryState.model_validate(value)
    except ValidationError as error:
        raise WorkerError("Stored retry state is invalid") from error


def _writer(attempt: RunAttempt, clock: Clock) -> LedgerWriter:
    return LedgerWriter(
        LedgerSettings(
            graph_version=attempt.graph_version,
            model_version="not-applicable@v1",
            prompt_version="not-applicable@v1",
            policy_version="invoice-retry@v2",
            _env_file=None,
        ),
        clock=clock,
    )


class AttemptStore(Protocol):
    async def begin(self, run_id: UUID, *, continuation: bool = False) -> RunAttempt | None: ...

    async def settle(self, attempt: RunAttempt, result: InvoiceGraphState) -> None: ...

    async def failed(
        self, attempt: RunAttempt, *, error_type: str, retryable: bool, terminal: bool
    ) -> None: ...


class PostgresAttemptStore:
    def __init__(
        self, connection: ConnectionFactory, config: RetryConfig, *, clock: Clock = utc_now
    ) -> None:
        self._connection = connection
        self._config = config
        self._clock = clock

    async def begin(self, run_id: UUID, *, continuation: bool = False) -> RunAttempt | None:
        now = self._clock()
        async with self._connection() as connection, connection.transaction():
            cursor = await connection.execute(
                "SELECT id, invoice_id, trace_id, graph_version, status, started_at, error "
                "FROM public.runs WHERE id = %s FOR UPDATE",
                (run_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RunNotFound("Invoice worker run is absent", run_id=run_id)
            if row["status"] in {"COMPLETED", "PAUSED"}:
                return None
            if row["status"] == "FAILED":
                raise RunInDeadLetter("Run is in the dead-letter queue")
            if row["status"] == "CANCELLED":
                raise WorkerError("Cancelled run cannot execute")
            started_at = row["started_at"]
            if (
                row["status"] == "RUNNING"
                and not continuation
                and started_at is not None
                and now - started_at < timedelta(seconds=self._config.running_lease_seconds)
            ):
                raise RunInProgress(
                    "Invoice worker already owns run", run_id=run_id, trace_id=row["trace_id"]
                )
            state = _state(row["error"])
            if state.attempts >= self._config.max_attempts:
                raise WorkerError("Retry budget is exhausted; run needs recovery")
            await connection.execute(
                "UPDATE public.runs SET status = 'RUNNING', started_at = %s, completed_at = NULL "
                "WHERE id = %s",
                (now, run_id),
            )
            return RunAttempt(
                run_id=run_id,
                invoice_id=row["invoice_id"],
                trace_id=row["trace_id"],
                graph_version=row["graph_version"],
                attempt=state.attempts + 1,
                cycle=state.cycle,
            )

    async def settle(self, attempt: RunAttempt, result: InvoiceGraphState) -> None:
        if (
            result.run_id != attempt.run_id
            or result.invoice_id != attempt.invoice_id
            or result.trace_id != attempt.trace_id
        ):
            raise WorkerError("Workflow result identity differs from the claimed run")
        if result.status not in {"awaiting_review", "completed", "rejected"}:
            raise WorkerError("Workflow did not reach a settled state")
        status = "PAUSED" if result.status == "awaiting_review" else "COMPLETED"
        completed_at = self._clock() if status == "COMPLETED" else None
        async with self._connection() as connection, connection.transaction():
            updated = await connection.execute(
                "UPDATE public.runs SET status = %s, completed_at = %s, error = NULL "
                "WHERE id = %s AND status = 'RUNNING'",
                (status, completed_at, attempt.run_id),
            )
            if updated.rowcount != 1:
                current = await connection.execute(
                    "SELECT status, error FROM public.runs WHERE id = %s", (attempt.run_id,)
                )
                row = await current.fetchone()
                if row is None or row["status"] != status or row["error"] is not None:
                    raise WorkerError("Claimed run changed before settlement")

    async def failed(
        self, attempt: RunAttempt, *, error_type: str, retryable: bool, terminal: bool
    ) -> None:
        now = self._clock()
        state = RetryState(
            attempts=attempt.attempt,
            cycle=attempt.cycle,
            error_type=error_type,
            retryable=retryable,
            dead_lettered=terminal,
        )
        async with self._connection() as connection, connection.transaction():
            updated = await connection.execute(
                "UPDATE public.runs SET status = %s, completed_at = %s, error = %s "
                "WHERE id = %s AND status = 'RUNNING'",
                (
                    "FAILED" if terminal else "RUNNING",
                    now if terminal else None,
                    psycopg.types.json.Jsonb(state.model_dump(mode="json")),
                    attempt.run_id,
                ),
            )
            if updated.rowcount != 1:
                current = await connection.execute(
                    "SELECT status, error FROM public.runs WHERE id = %s", (attempt.run_id,)
                )
                row = await current.fetchone()
                expected_status = "FAILED" if terminal else "RUNNING"
                if (
                    row is None
                    or row["status"] != expected_status
                    or row["error"] != state.model_dump(mode="json")
                ):
                    raise WorkerError("Claimed run changed before failure recording")
                return
            if terminal:
                await _writer(attempt, self._clock).append(
                    connection,
                    AppendEvent(
                        run_id=attempt.run_id,
                        invoice_id=attempt.invoice_id,
                        event_type="workflow.dead_lettered",
                        node="Worker",
                        actor_type="SYSTEM",
                        actor_id="invoiceops-retry-worker",
                        versions=VersionOverrides(policy_version=self._config.version),
                        payload={
                            "attempts": attempt.attempt,
                            "cycle": attempt.cycle,
                            "error_type": error_type,
                            "retryable": retryable,
                        },
                    ),
                    trace_id=attempt.trace_id,
                )
        logger.warning(
            "invoice_attempt_failed run_id=%s trace_id=%s attempt=%d retryable=%s terminal=%s "
            "error_type=%s",
            attempt.run_id,
            attempt.trace_id,
            attempt.attempt,
            retryable,
            terminal,
            error_type,
        )

    async def list_dead_letters(self, *, limit: int = 100) -> list[DeadLetter]:
        if not 1 <= limit <= 100:
            raise ValueError("Dead-letter page limit must be between 1 and 100")
        async with self._connection() as connection:
            cursor = await connection.execute(
                "SELECT id, invoice_id, error FROM public.runs WHERE status = 'FAILED' "
                "ORDER BY completed_at, id LIMIT %s",
                (limit,),
            )
            rows = await cursor.fetchall()
        result: list[DeadLetter] = []
        for row in rows:
            state = _state(row["error"])
            if not state.dead_lettered or state.error_type is None:
                raise WorkerError("Failed run lacks a dead-letter record")
            result.append(
                DeadLetter(
                    run_id=row["id"],
                    invoice_id=row["invoice_id"],
                    attempts=state.attempts,
                    cycle=state.cycle,
                    error_type=state.error_type,
                    retryable=bool(state.retryable),
                )
            )
        return result

    async def redrive(self, run_id: UUID, *, key: str, actor_id: str, reason: str) -> bool:
        if not _KEY.fullmatch(key) or not _ACTOR.fullmatch(actor_id):
            raise InvalidRedrive("Redrive requires a valid key and actor")
        if not reason.strip() or len(reason) > 2000:
            raise InvalidRedrive("Redrive requires a bounded reason")
        async with self._connection() as connection, connection.transaction():
            cursor = await connection.execute(
                "SELECT id, invoice_id, trace_id, graph_version, status, error "
                "FROM public.runs WHERE id = %s FOR UPDATE",
                (run_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RunNotFound("Dead-letter run is absent", run_id=run_id)
            prior = await connection.execute(
                "SELECT payload FROM public.ledger WHERE run_id = %s "
                "AND event_type = 'workflow.redriven' "
                "AND payload->>'idempotency_key' = %s LIMIT 1",
                (run_id, key),
            )
            previous = await prior.fetchone()
            if previous is not None:
                if (
                    previous["payload"].get("actor_id") != actor_id
                    or previous["payload"].get("reason") != reason
                ):
                    raise InvalidRedrive("Redrive key belongs to another request")
                return False
            if row["status"] != "FAILED":
                raise InvalidRedrive("Only a dead-lettered run can be redriven")
            state = _state(row["error"])
            if not state.dead_lettered:
                raise InvalidRedrive("Failed run lacks a dead-letter record")
            next_state = RetryState(cycle=state.cycle + 1)
            await connection.execute(
                "UPDATE public.runs SET status = 'QUEUED', started_at = NULL, completed_at = NULL, "
                "error = %s WHERE id = %s",
                (psycopg.types.json.Jsonb(next_state.model_dump(mode="json")), run_id),
            )
            attempt = RunAttempt(
                run_id=run_id,
                invoice_id=row["invoice_id"],
                trace_id=row["trace_id"],
                graph_version=row["graph_version"],
                attempt=1,
                cycle=next_state.cycle,
            )
            await _writer(attempt, self._clock).append(
                connection,
                AppendEvent(
                    run_id=run_id,
                    invoice_id=attempt.invoice_id,
                    event_type="workflow.redriven",
                    node="Worker",
                    actor_type="HUMAN",
                    actor_id=actor_id,
                    versions=VersionOverrides(policy_version=self._config.version),
                    payload={
                        "idempotency_key": key,
                        "actor_id": actor_id,
                        "reason": reason,
                        "from_cycle": state.cycle,
                        "to_cycle": next_state.cycle,
                    },
                ),
                trace_id=attempt.trace_id,
            )
        return True


class RetryWorker:
    def __init__(
        self,
        store: AttemptStore,
        run_once: RunOnce,
        config: RetryConfig | None = None,
        *,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._store = store
        self._run_once = run_once
        self._config = config if config is not None else RetryConfig()
        self._sleep = sleep

    async def process(self, run_id: UUID) -> InvoiceGraphState:
        continuation = False
        while True:
            attempt = await self._store.begin(run_id, continuation=continuation)
            if attempt is None:
                return await self._run_once(run_id)
            try:
                result = await self._run_once(run_id)
            except asyncio.CancelledError:
                raise
            except RunInProgress:
                raise
            except Exception as error:
                retryable = is_infrastructure_error(error)
                terminal = not retryable or attempt.attempt >= self._config.max_attempts
                await self._store.failed(
                    attempt,
                    error_type=type(root_error(error)).__name__,
                    retryable=retryable,
                    terminal=terminal,
                )
                if terminal:
                    raise
                await self._sleep(self._config.delay(attempt.attempt))
                continuation = True
            else:
                await self._store.settle(attempt, result)
                return result

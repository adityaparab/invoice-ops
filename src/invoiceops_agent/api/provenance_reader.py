"""Read-only, snapshot-consistent trace and invoice provenance pages."""

import logging
from datetime import datetime
from time import perf_counter
from typing import Protocol, cast
from uuid import UUID

import psycopg
from pydantic import ValidationError

from invoiceops_agent.api.read_store import ReadStoreUnavailable, connect_read_store
from invoiceops_agent.api.schemas.provenance import (
    InvoiceProvenancePage,
    RunTracePage,
    TraceEvent,
)
from invoiceops_agent.api.settings import ApiSettings
from invoiceops_agent.ledger.connection import LedgerConnection
from invoiceops_agent.ledger.errors import LedgerError
from invoiceops_agent.ledger.reader import LedgerReader
from invoiceops_agent.ledger.schemas import InvoiceCursor, RunCursor

logger = logging.getLogger(__name__)


class ProvenanceReadError(Exception):
    """Sanitized provenance read failure."""


class ProvenanceNotFound(ProvenanceReadError):
    """The requested run or invoice does not exist."""


class InvalidProvenanceCursor(ProvenanceReadError):
    """A continuation cursor is incomplete or invalid."""


class ProvenanceUnavailable(ProvenanceReadError):
    """The operational or audit store is unavailable."""


class ProvenanceReader(Protocol):
    async def for_run_trace(
        self, run_id: UUID, *, trace_id: str, limit: int = 50, after_sequence: int | None = None
    ) -> RunTracePage: ...

    async def for_invoice(
        self,
        invoice_id: UUID,
        *,
        trace_id: str,
        limit: int = 50,
        after_created_at: datetime | None = None,
        after_event_id: UUID | None = None,
    ) -> InvoiceProvenancePage: ...


class PostgresProvenanceReader:
    def __init__(self, settings: ApiSettings, *, ledger: LedgerReader | None = None) -> None:
        self._settings = settings
        self._ledger = ledger if ledger is not None else LedgerReader()

    async def for_run_trace(
        self, run_id: UUID, *, trace_id: str, limit: int = 50, after_sequence: int | None = None
    ) -> RunTracePage:
        started = perf_counter()
        try:
            after = (
                RunCursor(run_id=run_id, sequence=after_sequence)
                if after_sequence is not None
                else None
            )
        except ValidationError as error:
            raise InvalidProvenanceCursor("Run trace cursor is invalid") from error
        try:
            async with await connect_read_store(self._settings) as connection:
                async with connection.transaction():
                    await connection.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                    )
                    row = await (
                        await connection.execute(
                            "SELECT id, invoice_id, trace_id, status, graph_version, "
                            "started_at, completed_at FROM public.runs WHERE id = %s",
                            (run_id,),
                        )
                    ).fetchone()
                    if row is None:
                        raise ProvenanceNotFound("Run does not exist")
                    page = await self._ledger.for_run(
                        cast(LedgerConnection, connection),
                        run_id,
                        trace_id=trace_id,
                        limit=limit,
                        after=after,
                    )
            result = RunTracePage.model_validate(
                {
                    "run_id": row["id"],
                    "invoice_id": row["invoice_id"],
                    "trace_id": row["trace_id"],
                    "status": row["status"],
                    "graph_version": row["graph_version"],
                    "started_at": row["started_at"],
                    "completed_at": row["completed_at"],
                    "events": [
                        TraceEvent.model_validate(
                            event.model_dump(exclude={"payload", "run_id", "invoice_id"})
                        )
                        for event in page.events
                    ],
                    "next_cursor": page.next_cursor,
                }
            )
        except ProvenanceNotFound:
            raise
        except (
            ReadStoreUnavailable,
            psycopg.Error,
            LedgerError,
            ValidationError,
            KeyError,
        ) as error:
            logger.error(
                "run_trace_read_failed run_id=%s trace_id=%s error_type=%s",
                run_id,
                trace_id,
                type(error).__name__,
            )
            raise ProvenanceUnavailable("Run trace could not be read") from error
        logger.info(
            "run_trace_page_read run_id=%s trace_id=%s rows=%d duration_ms=%.3f",
            run_id,
            trace_id,
            len(result.events),
            (perf_counter() - started) * 1000,
        )
        return result

    async def for_invoice(
        self,
        invoice_id: UUID,
        *,
        trace_id: str,
        limit: int = 50,
        after_created_at: datetime | None = None,
        after_event_id: UUID | None = None,
    ) -> InvoiceProvenancePage:
        started = perf_counter()
        if (after_created_at is None) != (after_event_id is None):
            raise InvalidProvenanceCursor("Both provenance cursor fields are required together")
        try:
            after = (
                InvoiceCursor(
                    invoice_id=invoice_id,
                    created_at=after_created_at,
                    id=after_event_id,
                )
                if after_created_at is not None and after_event_id is not None
                else None
            )
        except ValidationError as error:
            raise InvalidProvenanceCursor("Invoice provenance cursor is invalid") from error
        try:
            async with await connect_read_store(self._settings) as connection:
                async with connection.transaction():
                    await connection.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                    )
                    row = await (
                        await connection.execute(
                            "SELECT id, status, source, created_at "
                            "FROM public.invoices WHERE id = %s",
                            (invoice_id,),
                        )
                    ).fetchone()
                    if row is None:
                        raise ProvenanceNotFound("Invoice does not exist")
                    page = await self._ledger.for_invoice(
                        cast(LedgerConnection, connection),
                        invoice_id,
                        trace_id=trace_id,
                        limit=limit,
                        after=after,
                    )
            result = InvoiceProvenancePage.model_validate(
                {
                    "invoice_id": row["id"],
                    "status": row["status"],
                    "source": row["source"],
                    "created_at": row["created_at"],
                    "events": page.events,
                    "next_cursor": page.next_cursor,
                }
            )
        except ProvenanceNotFound:
            raise
        except (
            ReadStoreUnavailable,
            psycopg.Error,
            LedgerError,
            ValidationError,
            KeyError,
        ) as error:
            logger.error(
                "invoice_provenance_read_failed invoice_id=%s trace_id=%s error_type=%s",
                invoice_id,
                trace_id,
                type(error).__name__,
            )
            raise ProvenanceUnavailable("Invoice provenance could not be read") from error
        logger.info(
            "invoice_provenance_page_read invoice_id=%s trace_id=%s rows=%d duration_ms=%.3f",
            invoice_id,
            trace_id,
            len(result.events),
            (perf_counter() - started) * 1000,
        )
        return result

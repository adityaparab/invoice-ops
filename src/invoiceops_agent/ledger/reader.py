"""Single-query keyset reads of append-only run and cross-run invoice history."""

import logging
from collections.abc import Mapping, Sequence
from time import perf_counter
from typing import LiteralString
from uuid import UUID

import psycopg
from pydantic import ValidationError

from invoiceops_agent.ledger.connection import LedgerConnection
from invoiceops_agent.ledger.errors import LedgerCursorMismatch, LedgerStorageError
from invoiceops_agent.ledger.schemas import (
    InvoiceCursor,
    InvoicePage,
    LedgerEvent,
    ReadQuery,
    RunCursor,
    RunPage,
)

logger = logging.getLogger(__name__)
_SELECT: LiteralString = (
    "SELECT id, run_id, invoice_id, sequence, event_type, node, actor_type, actor_id, "
    "graph_version, model_version, prompt_version, policy_version, payload, supersedes_id, "
    "created_at FROM public.ledger "
)


def _event_from_row(row: Mapping[str, object]) -> LedgerEvent:
    values = dict(row)
    values["versions"] = {
        name: values.pop(name)
        for name in ("graph_version", "model_version", "prompt_version", "policy_version")
    }
    return LedgerEvent.model_validate(values)


class LedgerReader:
    async def for_run(
        self,
        connection: LedgerConnection,
        run_id: UUID,
        *,
        trace_id: str,
        limit: int = 50,
        after: RunCursor | None = None,
    ) -> RunPage:
        query = ReadQuery(limit=limit, trace_id=trace_id)
        if after is not None and after.run_id != run_id:
            raise LedgerCursorMismatch(
                "Cursor belongs to another run", run_id=run_id, trace_id=trace_id
            )
        rows = await self._read(
            connection,
            _SELECT + "WHERE run_id = %s AND sequence > %s ORDER BY sequence ASC LIMIT %s",
            (run_id, after.sequence if after is not None else 0, query.limit + 1),
            query=query,
            run_id=run_id,
            invoice_id=None,
        )
        events = rows[: query.limit]
        cursor = (
            RunCursor(run_id=run_id, sequence=events[-1].sequence)
            if len(rows) > query.limit
            else None
        )
        return RunPage(events=events, next_cursor=cursor)

    async def for_invoice(
        self,
        connection: LedgerConnection,
        invoice_id: UUID,
        *,
        trace_id: str,
        limit: int = 50,
        after: InvoiceCursor | None = None,
    ) -> InvoicePage:
        query = ReadQuery(limit=limit, trace_id=trace_id)
        if after is not None and after.invoice_id != invoice_id:
            raise LedgerCursorMismatch(
                "Cursor belongs to another invoice", run_id=None, trace_id=trace_id
            )
        if after is None:
            statement = _SELECT + "WHERE invoice_id = %s ORDER BY created_at ASC, id ASC LIMIT %s"
            params: Sequence[object] = (invoice_id, query.limit + 1)
        else:
            statement = (
                _SELECT + "WHERE invoice_id = %s AND (created_at, id) > (%s, %s) "
                "ORDER BY created_at ASC, id ASC LIMIT %s"
            )
            params = (invoice_id, after.created_at, after.id, query.limit + 1)
        rows = await self._read(
            connection,
            statement,
            params,
            query=query,
            run_id=None,
            invoice_id=invoice_id,
        )
        events = rows[: query.limit]
        cursor = (
            InvoiceCursor(invoice_id=invoice_id, created_at=events[-1].created_at, id=events[-1].id)
            if len(rows) > query.limit
            else None
        )
        return InvoicePage(events=events, next_cursor=cursor)

    @staticmethod
    async def _read(
        connection: LedgerConnection,
        statement: LiteralString,
        params: Sequence[object],
        *,
        query: ReadQuery,
        run_id: UUID | None,
        invoice_id: UUID | None,
    ) -> list[LedgerEvent]:
        started = perf_counter()
        try:
            cursor = await connection.execute(statement, params)
            events = [_event_from_row(row) for row in await cursor.fetchall()]
        except (psycopg.Error, ValidationError, KeyError) as error:
            logger.error(
                "ledger_read_failed run_id=%s invoice_id=%s trace_id=%s error_type=%s",
                run_id,
                invoice_id,
                query.trace_id,
                type(error).__name__,
            )
            raise LedgerStorageError(
                "Ledger history could not be read", run_id=run_id, trace_id=query.trace_id
            ) from error
        logger.info(
            "ledger_page_read run_id=%s invoice_id=%s trace_id=%s rows=%d duration_ms=%.3f",
            run_id,
            invoice_id,
            query.trace_id,
            len(events),
            (perf_counter() - started) * 1000,
        )
        return events

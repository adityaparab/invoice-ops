"""Audit run history with a restricted read connection and stable keyset pages."""

import logging
from time import perf_counter
from typing import Protocol, cast
from uuid import UUID

import psycopg
from pydantic import ValidationError

from invoiceops_agent.api.read_store import ReadStoreUnavailable, connect_read_store
from invoiceops_agent.api.schemas.audit import AuditRunPage
from invoiceops_agent.api.settings import ApiSettings
from invoiceops_agent.ledger.connection import LedgerConnection
from invoiceops_agent.ledger.errors import LedgerError
from invoiceops_agent.ledger.reader import LedgerReader
from invoiceops_agent.ledger.schemas import RunCursor

logger = logging.getLogger(__name__)


class AuditReadError(Exception):
    """Sanitized audit read failure."""


class AuditRunNotFound(AuditReadError):
    """The requested run does not exist."""


class AuditReadUnavailable(AuditReadError):
    """The operational or audit store is unavailable."""


class AuditReader(Protocol):
    async def for_run(
        self, run_id: UUID, *, trace_id: str, limit: int = 50, after_sequence: int | None = None
    ) -> AuditRunPage: ...


class PostgresAuditReader:
    def __init__(self, settings: ApiSettings, *, ledger: LedgerReader | None = None) -> None:
        self._settings = settings
        self._ledger = ledger if ledger is not None else LedgerReader()

    async def for_run(
        self, run_id: UUID, *, trace_id: str, limit: int = 50, after_sequence: int | None = None
    ) -> AuditRunPage:
        started = perf_counter()
        try:
            async with await connect_read_store(self._settings) as connection:
                async with connection.transaction():
                    await connection.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                    )
                    row = await (
                        await connection.execute(
                            "SELECT id, invoice_id, trace_id, status "
                            "FROM public.runs WHERE id = %s",
                            (run_id,),
                        )
                    ).fetchone()
                    if row is None:
                        raise AuditRunNotFound("Run does not exist")
                    page = await self._ledger.for_run(
                        cast(LedgerConnection, connection),
                        run_id,
                        trace_id=trace_id,
                        limit=limit,
                        after=RunCursor(run_id=run_id, sequence=after_sequence)
                        if after_sequence is not None
                        else None,
                    )
            result = AuditRunPage.model_validate(
                {
                    "run_id": row["id"],
                    "invoice_id": row["invoice_id"],
                    "trace_id": row["trace_id"],
                    "status": row["status"],
                    "events": page.events,
                    "next_cursor": page.next_cursor,
                }
            )
        except AuditRunNotFound:
            raise
        except (ReadStoreUnavailable, psycopg.Error, LedgerError, ValidationError) as error:
            logger.error(
                "audit_run_read_failed run_id=%s trace_id=%s error_type=%s",
                run_id,
                trace_id,
                type(error).__name__,
            )
            raise AuditReadUnavailable("Run audit history could not be read") from error
        logger.info(
            "audit_run_page_read run_id=%s trace_id=%s rows=%d after_sequence=%s duration_ms=%.3f",
            run_id,
            trace_id,
            len(result.events),
            after_sequence,
            (perf_counter() - started) * 1000,
        )
        return result

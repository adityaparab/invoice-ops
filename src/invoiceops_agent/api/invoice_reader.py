"""Bounded async invoice read projections from operational rows and audited evidence."""

import base64
import binascii
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from pydantic import JsonValue, ValidationError

from invoiceops_agent.api.schemas.invoice_read import (
    InvoiceDetail,
    InvoiceException,
    InvoiceListQuery,
    InvoicePage,
    InvoiceSummary,
    PendingProposal,
)
from invoiceops_agent.api.settings import ApiSettings

logger = logging.getLogger(__name__)

_QUEUE_SQL = """
SELECT i.id, r.id AS run_id, i.status, r.status AS run_status, i.source, i.content_type,
       x.extraction->'vendor_name'->>'value' AS vendor_name,
       COALESCE(i.invoice_number, x.extraction->'invoice_number'->>'value') AS invoice_number,
       COALESCE(i.po_number, x.extraction->'po_number'->>'value') AS po_number,
       COALESCE(i.currency, x.extraction->'currency'->>'value') AS currency,
       COALESCE(i.total_amount::text, x.extraction->'total_amount'->>'value') AS total_amount,
       e.id AS exception_id, e.exception_type, e.priority AS exception_priority,
       e.sla_due_at AS exception_sla_due_at, i.created_at
FROM public.invoices i
JOIN LATERAL (
    SELECT id, status FROM public.runs WHERE invoice_id = i.id
    ORDER BY created_at DESC, id DESC LIMIT 1
) r ON TRUE
LEFT JOIN LATERAL (
    SELECT payload->'result'->'extraction' AS extraction FROM public.ledger
    WHERE run_id = r.id AND event_type = 'extraction.completed'
    ORDER BY sequence DESC LIMIT 1
) x ON TRUE
LEFT JOIN LATERAL (
    SELECT id, exception_type, priority, sla_due_at FROM public.exceptions
    WHERE run_id = r.id ORDER BY created_at DESC, id DESC LIMIT 1
) e ON TRUE
"""
_EVIDENCE_TYPES = [
    "extraction.completed",
    "extraction.escalated",
    "validation.completed",
    "matching.completed",
    "similarity.completed",
    "classification.completed",
    "policy.completed",
    "gate.completed",
    "triage.prepared",
    "review.recorded",
]


class InvoiceReadError(Exception):
    """Sanitized read-side failure."""


class InvoiceNotFound(InvoiceReadError):
    """The requested invoice does not exist."""


class InvalidInvoiceCursor(InvoiceReadError):
    """The queue cursor cannot be decoded safely."""


class InvoiceReadUnavailable(InvoiceReadError):
    """The operational read store is unavailable."""


class InvoiceReader(Protocol):
    async def list(self, query: InvoiceListQuery) -> InvoicePage: ...

    async def detail(self, invoice_id: UUID) -> InvoiceDetail: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


def _encode_cursor(item: InvoiceSummary) -> str:
    value = f"{item.created_at.isoformat()}|{item.id}".encode("ascii")
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, UUID]:
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        ).decode("ascii")
        timestamp, raw_id = decoded.split("|", 1)
        created_at = datetime.fromisoformat(timestamp)
        if created_at.utcoffset() is None:
            raise ValueError("Cursor timestamp must have an offset")
        return created_at, UUID(raw_id)
    except (ValueError, UnicodeError, binascii.Error) as error:
        raise InvalidInvoiceCursor("Invoice queue cursor is invalid") from error


class PostgresInvoiceReader:
    def __init__(self, settings: ApiSettings, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._settings = settings
        self._clock = clock

    async def _connect(self) -> psycopg.AsyncConnection[dict[str, object]]:
        if self._settings.postgres_dsn is None:
            raise InvoiceReadUnavailable("Invoice reads are not configured")
        try:
            return await psycopg.AsyncConnection.connect(
                self._settings.postgres_dsn.get_secret_value(),
                autocommit=True,
                row_factory=dict_row,
                connect_timeout=5,
                options="-c statement_timeout=10000 -c lock_timeout=10000",
            )
        except psycopg.Error as error:
            raise InvoiceReadUnavailable("Invoice read connection failed") from error

    async def list(self, query: InvoiceListQuery) -> InvoicePage:
        clauses: list[str] = []
        parameters: list[object] = []
        for column, value in (
            ("status", query.status),
            ("run_status", query.run_status),
            ("source", query.source),
        ):
            if value is not None:
                clauses.append(f"{column} = %s")
                parameters.append(value)
        if query.exception_only:
            clauses.append("exception_id IS NOT NULL")
        if query.min_priority is not None:
            clauses.append("exception_priority >= %s")
            parameters.append(query.min_priority)
        if query.cursor is not None:
            created_at, invoice_id = _decode_cursor(query.cursor)
            clauses.append("(created_at, id) < (%s, %s)")
            parameters.extend((created_at, invoice_id))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = (
            "WITH queue AS ("
            + _QUEUE_SQL
            + ") SELECT * FROM queue"
            + where
            + " ORDER BY created_at DESC, id DESC LIMIT %s"
        )
        parameters.append(query.limit + 1)
        try:
            async with await self._connect() as connection:
                rows = await (await connection.execute(sql, parameters)).fetchall()
        except psycopg.Error as error:
            logger.error("invoice_queue_read_failed error_type=%s", type(error).__name__)
            raise InvoiceReadUnavailable("Invoice queue read failed") from error
        try:
            items = [InvoiceSummary.model_validate(row) for row in rows[: query.limit]]
        except ValidationError as error:
            raise InvoiceReadUnavailable("Invoice queue contains invalid stored data") from error
        return InvoicePage(
            items=items,
            next_cursor=_encode_cursor(items[-1]) if len(rows) > query.limit and items else None,
        )

    async def detail(self, invoice_id: UUID) -> InvoiceDetail:
        try:
            async with await self._connect() as connection:
                cursor = await connection.execute(
                    "WITH queue AS (" + _QUEUE_SQL + ") SELECT * FROM queue WHERE id = %s",
                    (invoice_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise InvoiceNotFound("Invoice does not exist")
                summary = InvoiceSummary.model_validate(row)
                exception: InvoiceException | None = None
                pending_proposal: PendingProposal | None = None
                if summary.exception_id is not None:
                    exception_row = await (
                        await connection.execute(
                            "SELECT id, exception_type, status, priority, sla_due_at, "
                            "assigned_to, evidence, recommendation, created_at "
                            "FROM public.exceptions WHERE id = %s",
                            (summary.exception_id,),
                        )
                    ).fetchone()
                    if exception_row is not None:
                        exception = InvoiceException.model_validate(exception_row)
                        if exception.status == "IN_REVIEW":
                            proposal_row = await (
                                await connection.execute(
                                    "SELECT id, action, rationale, reason_code, actor_id, "
                                    "created_at "
                                    "FROM public.decisions WHERE exception_id = %s "
                                    "AND supersedes_id IS NULL "
                                    "ORDER BY created_at DESC, id DESC LIMIT 1",
                                    (exception.id,),
                                )
                            ).fetchone()
                            if proposal_row is not None:
                                pending_proposal = PendingProposal.model_validate(proposal_row)
                evidence_rows = await (
                    await connection.execute(
                        "SELECT event_type, payload FROM public.ledger WHERE run_id = %s "
                        "AND event_type = ANY(%s::text[]) ORDER BY sequence",
                        (summary.run_id, _EVIDENCE_TYPES),
                    )
                ).fetchall()
        except InvoiceNotFound:
            raise
        except (psycopg.Error, ValidationError) as error:
            logger.error("invoice_detail_read_failed error_type=%s", type(error).__name__)
            raise InvoiceReadUnavailable("Invoice detail read failed") from error
        evidence: dict[str, dict[str, JsonValue]] = {}
        for item in evidence_rows:
            payload = item["payload"]
            if not isinstance(payload, dict):
                raise InvoiceReadUnavailable("Invoice evidence is invalid")
            evidence[str(item["event_type"])] = payload
        return InvoiceDetail(
            invoice=summary,
            exception=exception,
            pending_proposal=pending_proposal,
            evidence=evidence,
            read_at=self._clock(),
        )

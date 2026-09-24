"""Consistent, bounded manager dashboard aggregates from operational evidence."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from time import perf_counter
from typing import Protocol

import psycopg
from pydantic import ValidationError

from invoiceops_agent.api.read_store import ReadStoreUnavailable, connect_read_store
from invoiceops_agent.api.schemas.dashboard import (
    AgingCounts,
    DailyVolume,
    DashboardSummary,
    ExceptionCount,
)
from invoiceops_agent.api.settings import ApiSettings

logger = logging.getLogger(__name__)
_RESOLVED = frozenset({"APPROVED", "REJECTED", "RETURNED", "ARCHIVED"})
_LATEST_EXCEPTION = """
WITH latest AS (
    SELECT DISTINCT ON (e.invoice_id) e.exception_type, e.status, e.created_at, e.sla_due_at
    FROM public.exceptions e JOIN public.invoices i ON i.id = e.invoice_id
    WHERE i.created_at >= %s AND i.created_at < %s
    ORDER BY e.invoice_id, e.created_at DESC, e.id DESC
)
"""
_COST_SQL = """
WITH scoped AS (
    SELECT id FROM public.invoices WHERE created_at >= %s AND created_at < %s
), raw_costs AS (
    SELECT l.invoice_id, call->>'cost_usd' AS cost_text
    FROM public.ledger l JOIN scoped s ON s.id = l.invoice_id
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(l.payload->'result'->'calls') = 'array'
             THEN l.payload->'result'->'calls' ELSE '[]'::jsonb END
    ) call
    WHERE l.event_type IN ('extraction.completed', 'extraction.escalated')
    UNION ALL
    SELECT l.invoice_id, l.payload->'triage'->>'cost_usd'
    FROM public.ledger l JOIN scoped s ON s.id = l.invoice_id
    WHERE l.event_type = 'triage.prepared'
), valid_costs AS (
    SELECT invoice_id,
           CASE WHEN cost_text ~ '^[0-9]+([.][0-9]+)?$'
                THEN cost_text::numeric ELSE NULL END AS amount
    FROM raw_costs
)
SELECT COUNT(DISTINCT invoice_id) FILTER (WHERE amount IS NOT NULL) AS observed,
       SUM(amount) AS total_cost
FROM valid_costs
"""


class DashboardUnavailable(Exception):
    """The read-only dashboard projection is unavailable."""


class DashboardReader(Protocol):
    async def summary(self, period_days: int) -> DashboardSummary: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


def _count(value: object) -> int:
    if not isinstance(value, int) or value < 0:
        raise ValueError("Dashboard count must be a non-negative integer")
    return value


class PostgresDashboardReader:
    def __init__(self, settings: ApiSettings, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._settings = settings
        self._clock = clock

    async def summary(self, period_days: int) -> DashboardSummary:
        if period_days < 1 or period_days > 90:
            raise ValueError("Dashboard period must be from 1 to 90 days")
        started = perf_counter()
        instant = self._clock()
        if instant.utcoffset() is None:
            raise ValueError("Dashboard clock must be timezone aware")
        as_of = instant.astimezone(UTC)
        start_day = as_of.date() - timedelta(days=period_days - 1)
        cutoff = datetime.combine(start_day, time.min, tzinfo=UTC)
        parameters = (cutoff, as_of)
        try:
            async with await connect_read_store(self._settings) as connection:
                async with connection.transaction():
                    await connection.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                    )
                    statuses = await (
                        await connection.execute(
                            "SELECT status, count(*) AS count FROM public.invoices "
                            "WHERE created_at >= %s AND created_at < %s GROUP BY status",
                            parameters,
                        )
                    ).fetchall()
                    volumes = await (
                        await connection.execute(
                            "SELECT (created_at AT TIME ZONE 'UTC')::date AS day, "
                            "count(*) AS count FROM public.invoices "
                            "WHERE created_at >= %s AND created_at < %s GROUP BY day",
                            parameters,
                        )
                    ).fetchall()
                    auto = await (
                        await connection.execute(
                            "SELECT count(DISTINCT l.invoice_id) AS count FROM public.ledger l "
                            "JOIN public.invoices i ON i.id = l.invoice_id "
                            "WHERE i.created_at >= %s AND i.created_at < %s "
                            "AND l.event_type = 'approval.auto_granted' "
                            "AND i.status = ANY(%s::text[])",
                            (*parameters, list(_RESOLVED)),
                        )
                    ).fetchone()
                    exceptions = await (
                        await connection.execute(
                            _LATEST_EXCEPTION
                            + "SELECT exception_type AS code, count(*) AS count FROM latest "
                            "GROUP BY exception_type ORDER BY count DESC, exception_type",
                            parameters,
                        )
                    ).fetchall()
                    aging = await (
                        await connection.execute(
                            _LATEST_EXCEPTION
                            + "SELECT count(*) FILTER (WHERE status <> 'RESOLVED') AS open_count, "
                            "count(*) FILTER (WHERE status <> 'RESOLVED' AND created_at > %s) "
                            "AS under_24_hours, "
                            "count(*) FILTER (WHERE status <> 'RESOLVED' AND created_at <= %s "
                            "AND created_at > %s) AS one_to_three_days, "
                            "count(*) FILTER (WHERE status <> 'RESOLVED' AND created_at <= %s) "
                            "AS over_three_days, "
                            "count(*) FILTER (WHERE status <> 'RESOLVED' AND sla_due_at < %s) "
                            "AS sla_overdue FROM latest",
                            (
                                *parameters,
                                as_of - timedelta(days=1),
                                as_of - timedelta(days=1),
                                as_of - timedelta(days=3),
                                as_of - timedelta(days=3),
                                as_of,
                            ),
                        )
                    ).fetchone()
                    cost = await (await connection.execute(_COST_SQL, parameters)).fetchone()
        except (psycopg.Error, ReadStoreUnavailable) as error:
            logger.error("dashboard_read_failed error_type=%s", type(error).__name__)
            raise DashboardUnavailable("Dashboard data is unavailable") from error
        try:
            if aging is None:
                raise ValueError("Dashboard aging aggregate is missing")
            counts = {str(row["status"]): _count(row["count"]) for row in statuses}
            invoice_count = sum(counts.values())
            resolved_count = sum(counts.get(status, 0) for status in _RESOLVED)
            auto_count = _count(auto["count"]) if auto is not None else 0
            daily = {row["day"]: _count(row["count"]) for row in volumes}
            observed = _count(cost["observed"]) if cost is not None else 0
            total_cost = cost["total_cost"] if cost is not None else None
            if total_cost is not None and not isinstance(total_cost, Decimal):
                raise ValueError("Dashboard cost must be an exact Decimal")
            summary = DashboardSummary(
                as_of=as_of,
                period_days=period_days,
                invoice_count=invoice_count,
                resolved_count=resolved_count,
                auto_approved_count=auto_count,
                stp_rate=Decimal(auto_count) / Decimal(resolved_count) if resolved_count else None,
                open_exception_count=_count(aging["open_count"]),
                aging=AgingCounts.model_validate(
                    {
                        name: aging[name]
                        for name in (
                            "under_24_hours",
                            "one_to_three_days",
                            "over_three_days",
                            "sla_overdue",
                        )
                    }
                ),
                cost_observed_invoices=observed,
                total_observed_cost_usd=total_cost,
                cost_per_observed_invoice_usd=total_cost / observed
                if total_cost is not None and observed
                else None,
                cost_coverage="COMPLETE"
                if observed == invoice_count and invoice_count
                else "PARTIAL"
                if observed
                else "UNAVAILABLE",
                volume_by_day=tuple(
                    DailyVolume(
                        day=start_day + timedelta(days=offset),
                        invoices=daily.get(start_day + timedelta(days=offset), 0),
                    )
                    for offset in range(period_days)
                ),
                exception_types=tuple(ExceptionCount.model_validate(row) for row in exceptions),
            )
        except (TypeError, ValueError, ValidationError, ArithmeticError) as error:
            logger.error("dashboard_data_invalid error_type=%s", type(error).__name__)
            raise DashboardUnavailable("Dashboard data is invalid") from error
        logger.info(
            "dashboard_read period_days=%d invoice_count=%d duration_ms=%.3f",
            period_days,
            summary.invoice_count,
            (perf_counter() - started) * 1000,
        )
        return summary

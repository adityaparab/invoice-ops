"""Dashboard aggregates use the restricted role and exact audited costs."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import SecretStr
from tests.integration.test_ledger import ledger_runtime_dsn as ledger_runtime_dsn
from tests.integration.test_ledger import runtime_connection, writer

from invoiceops_agent.api.dashboard_reader import PostgresDashboardReader
from invoiceops_agent.api.settings import ApiSettings
from invoiceops_agent.ledger.schemas import AppendEvent

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
AUTO_INVOICE = UUID(int=600)
AUTO_RUN = UUID(int=601)
REVIEW_INVOICE = UUID(int=602)
REVIEW_RUN = UUID(int=603)


async def test_dashboard_totals_aging_and_cost_coverage(ledger_runtime_dsn: str) -> None:
    async with runtime_connection(ledger_runtime_dsn) as connection, connection.transaction():
        for invoice_id, run_id, status, run_status in (
            (AUTO_INVOICE, AUTO_RUN, "APPROVED", "COMPLETED"),
            (REVIEW_INVOICE, REVIEW_RUN, "NEEDS_REVIEW", "PAUSED"),
        ):
            await connection.execute(
                "INSERT INTO public.invoices "
                "(id, content_hash, raw_ref, content_type, source, status, created_at) "
                "VALUES (%s, %s, %s, 'application/pdf', 'UPLOAD', %s, "
                "'2030-01-15T08:00:00Z')",
                (invoice_id, f"{invoice_id.int:064x}", f"sha256/{invoice_id}", status),
            )
            await connection.execute(
                "INSERT INTO public.runs (id, invoice_id, graph_version, trace_id, status) "
                "VALUES (%s, %s, 'graph@v1', %s, %s)",
                (run_id, invoice_id, f"{run_id.int:032x}", run_status),
            )
        await connection.execute(
            "INSERT INTO public.exceptions "
            "(id, run_id, invoice_id, exception_type, priority, sla_due_at, "
            "evidence, recommendation, created_at) "
            "VALUES (%s, %s, %s, 'PRICE_VARIANCE', 2, '2030-01-15T09:00:00Z', "
            "'{}', '{}', '2030-01-14T00:00:00Z')",
            (UUID(int=604), REVIEW_RUN, REVIEW_INVOICE),
        )
        for invoice_id, run_id, event_type, payload in (
            (AUTO_INVOICE, AUTO_RUN, "approval.auto_granted", {}),
            (
                AUTO_INVOICE,
                AUTO_RUN,
                "extraction.completed",
                {"result": {"calls": [{"cost_usd": "0.02"}]}},
            ),
            (
                AUTO_INVOICE,
                AUTO_RUN,
                "similarity.completed",
                {"gateway_cost_usd": "0.01"},
            ),
            (REVIEW_INVOICE, REVIEW_RUN, "triage.prepared", {"triage": {"cost_usd": "0.04"}}),
        ):
            await writer().append(
                connection,
                AppendEvent(
                    run_id=run_id,
                    invoice_id=invoice_id,
                    event_type=event_type,
                    actor_type="SYSTEM",
                    actor_id="synthetic-dashboard-test",
                    payload=payload,
                ),
                trace_id="a" * 32,
            )
    summary = await PostgresDashboardReader(
        ApiSettings(postgres_dsn=SecretStr(ledger_runtime_dsn)),
        clock=lambda: datetime(2030, 1, 15, 12, tzinfo=UTC),
    ).summary(2)
    assert (summary.invoice_count, summary.resolved_count, summary.auto_approved_count) == (2, 1, 1)
    assert summary.stp_rate == Decimal("1")
    assert summary.open_exception_count == 1
    assert summary.aging.one_to_three_days == 1
    assert summary.aging.sla_overdue == 1
    assert summary.cost_observed_invoices == 2
    assert summary.total_observed_cost_usd == Decimal("0.07")
    assert summary.cost_per_observed_invoice_usd == Decimal("0.035")
    assert summary.cost_coverage == "COMPLETE"
    assert [day.invoices for day in summary.volume_by_day] == [0, 2]
    assert [(item.code, item.count) for item in summary.exception_types] == [("PRICE_VARIANCE", 1)]
    async with runtime_connection(ledger_runtime_dsn) as connection, connection.transaction():
        await connection.execute(
            "INSERT INTO public.invoices "
            "(id, content_hash, raw_ref, content_type, source, status, created_at) "
            "VALUES (%s, %s, 'sha256/no-cost', 'application/pdf', 'UPLOAD', 'QUEUED', "
            "'2030-01-15T09:00:00Z')",
            (UUID(int=605), f"{605:064x}"),
        )
    partial = await PostgresDashboardReader(
        ApiSettings(postgres_dsn=SecretStr(ledger_runtime_dsn)),
        clock=lambda: datetime(2030, 1, 15, 12, tzinfo=UTC),
    ).summary(2)
    assert partial.invoice_count == 3
    assert partial.cost_observed_invoices == 2
    assert partial.cost_coverage == "PARTIAL"
    assert partial.cost_per_observed_invoice_usd == Decimal("0.035")
    empty = await PostgresDashboardReader(
        ApiSettings(postgres_dsn=SecretStr(ledger_runtime_dsn)),
        clock=lambda: datetime(2030, 1, 17, 12, tzinfo=UTC),
    ).summary(1)
    assert empty.invoice_count == 0
    assert empty.stp_rate is None
    assert empty.cost_coverage == "UNAVAILABLE"
    assert empty.cost_per_observed_invoice_usd is None

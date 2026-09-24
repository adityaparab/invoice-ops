"""Operational invoice reads include committed review and extraction evidence."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import SecretStr
from tests.integration.support import INVOICE_ID, RUN_ID
from tests.integration.test_ledger import ledger_runtime_dsn as ledger_runtime_dsn
from tests.integration.test_ledger import runtime_connection, writer

from invoiceops_agent.api.invoice_reader import InvoiceNotFound, PostgresInvoiceReader
from invoiceops_agent.api.schemas.invoice_read import InvoiceListQuery
from invoiceops_agent.api.settings import ApiSettings
from invoiceops_agent.ledger.schemas import AppendEvent

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
SECOND_INVOICE = UUID(int=60)
SECOND_RUN = UUID(int=61)
EXCEPTION = UUID(int=62)


async def test_queue_filters_keyset_and_aggregate_detail(ledger_runtime_dsn: str) -> None:
    due_at = datetime(2026, 9, 25, tzinfo=UTC)
    async with runtime_connection(ledger_runtime_dsn) as connection, connection.transaction():
        await connection.execute(
            "INSERT INTO public.invoices "
            "(id, content_hash, raw_ref, content_type, source, status, created_at) "
            "VALUES (%s, %s, 'sha256/second', 'application/pdf', 'EMAIL', "
            "'NEEDS_REVIEW', '2030-01-01T00:00:00Z')",
            (SECOND_INVOICE, "b" * 64),
        )
        await connection.execute(
            "INSERT INTO public.runs (id, invoice_id, graph_version, trace_id, status) "
            "VALUES (%s, %s, 'graph@v1', %s, 'PAUSED')",
            (SECOND_RUN, SECOND_INVOICE, "a" * 32),
        )
        await connection.execute(
            "INSERT INTO public.exceptions "
            "(id, run_id, invoice_id, exception_type, priority, sla_due_at, "
            "evidence, recommendation) "
            "VALUES (%s, %s, %s, 'BANK_CHANGE', 3, %s, '{}', '{\"recommendation\":\"REVIEW\"}')",
            (EXCEPTION, SECOND_RUN, SECOND_INVOICE, due_at),
        )
        await writer().append(
            connection,
            AppendEvent(
                run_id=SECOND_RUN,
                invoice_id=SECOND_INVOICE,
                event_type="extraction.completed",
                node="Extraction",
                actor_type="AGENT",
                actor_id="synthetic-test-agent",
                payload={
                    "result": {
                        "extraction": {
                            "vendor_name": {"value": "Synthetic Supplier"},
                            "invoice_number": {"value": "SYN-01"},
                            "total_amount": {"value": "123.45"},
                            "currency": {"value": "USD"},
                        }
                    }
                },
            ),
            trace_id="a" * 32,
        )
    reader = PostgresInvoiceReader(
        ApiSettings(postgres_dsn=SecretStr(ledger_runtime_dsn)),
        clock=lambda: datetime(2026, 9, 24, tzinfo=UTC),
    )
    first = await reader.list(InvoiceListQuery(limit=1))
    assert len(first.items) == 1 and first.items[0].id == SECOND_INVOICE
    assert first.next_cursor is not None
    second = await reader.list(InvoiceListQuery(limit=1, cursor=first.next_cursor))
    assert [item.id for item in second.items] == [INVOICE_ID]
    assert second.next_cursor is None
    filtered = await reader.list(
        InvoiceListQuery(
            status="NEEDS_REVIEW",
            run_status="PAUSED",
            source="EMAIL",
            exception_only=True,
            min_priority=3,
        )
    )
    assert [item.id for item in filtered.items] == [SECOND_INVOICE]
    assert filtered.items[0].vendor_name == "Synthetic Supplier"
    assert filtered.items[0].total_amount == Decimal("123.45")
    detail = await reader.detail(SECOND_INVOICE)
    assert detail.exception is not None and detail.exception.id == EXCEPTION
    assert detail.exception.sla_due_at == due_at
    assert detail.evidence["extraction.completed"]["result"] is not None
    assert detail.read_at == datetime(2026, 9, 24, tzinfo=UTC)
    with pytest.raises(InvoiceNotFound):
        await reader.detail(UUID(int=999))
    assert RUN_ID != SECOND_RUN

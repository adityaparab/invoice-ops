"""Offline provenance authorization, cursor, and problem response tests."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import SecretStr
from tests.unit.test_api import assert_problem, client_for

from invoiceops_agent.api.app import create_app
from invoiceops_agent.api.provenance_reader import (
    InvalidProvenanceCursor,
    PostgresProvenanceReader,
    ProvenanceNotFound,
)
from invoiceops_agent.api.schemas.provenance import InvoiceProvenancePage, RunTracePage
from invoiceops_agent.api.settings import ApiSettings

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
RUN_ID = UUID(int=71)
INVOICE_ID = UUID(int=72)
EVENT_ID = UUID(int=73)
TRACE_ID = "a" * 32
WHEN = datetime(2026, 9, 24, 12, tzinfo=UTC)


class StubProvenanceReader:
    def __init__(self) -> None:
        self.run_calls: list[tuple[UUID, str, int, int | None]] = []
        self.invoice_calls: list[tuple[UUID, str, int, datetime | None, UUID | None]] = []

    async def for_run_trace(
        self, run_id: UUID, *, trace_id: str, limit: int = 50, after_sequence: int | None = None
    ) -> RunTracePage:
        self.run_calls.append((run_id, trace_id, limit, after_sequence))
        if run_id != RUN_ID:
            raise ProvenanceNotFound("Run does not exist")
        return RunTracePage(
            run_id=run_id,
            invoice_id=INVOICE_ID,
            trace_id=TRACE_ID,
            status="PAUSED",
            graph_version="graph@v1",
            started_at=None,
            completed_at=None,
            events=[],
            next_cursor=None,
        )

    async def for_invoice(
        self,
        invoice_id: UUID,
        *,
        trace_id: str,
        limit: int = 50,
        after_created_at: datetime | None = None,
        after_event_id: UUID | None = None,
    ) -> InvoiceProvenancePage:
        self.invoice_calls.append((invoice_id, trace_id, limit, after_created_at, after_event_id))
        if invoice_id != INVOICE_ID:
            raise ProvenanceNotFound("Invoice does not exist")
        return InvoiceProvenancePage(
            invoice_id=invoice_id,
            status="NEEDS_REVIEW",
            source="UPLOAD",
            created_at=WHEN,
            events=[],
            next_cursor=None,
        )


async def test_run_trace_requires_auditor_and_passes_bounded_cursor() -> None:
    reader = StubProvenanceReader()
    settings = ApiSettings(
        auditor_token=SecretStr("synthetic-auditor-token"),
        analyst_token=SecretStr("synthetic-analyst-token"),
        service_token=SecretStr("synthetic-service-token"),
    )
    async with client_for(create_app(settings, provenance_reader=reader)) as client:
        headers = {"Authorization": "Bearer synthetic-auditor-token", "X-Trace-ID": TRACE_ID}
        accepted = await client.get(
            f"/v1/runs/{RUN_ID}/trace?limit=1&after_sequence=3", headers=headers
        )
        denied = await client.get(
            f"/v1/runs/{RUN_ID}/trace",
            headers={"Authorization": "Bearer synthetic-analyst-token"},
        )
        service = await client.get(
            f"/v1/runs/{RUN_ID}/trace",
            headers={"Authorization": "Bearer synthetic-service-token"},
        )
        invalid = await client.get(f"/v1/runs/{RUN_ID}/trace?limit=201", headers=headers)
        missing = await client.get(f"/v1/runs/{UUID(int=99)}/trace", headers=headers)
    assert accepted.status_code == 200
    assert accepted.json()["graph_version"] == "graph@v1"
    assert reader.run_calls == [(RUN_ID, TRACE_ID, 1, 3), (UUID(int=99), TRACE_ID, 50, None)]
    assert_problem(denied, 403)
    assert_problem(service, 401)
    assert_problem(invalid, 422)
    assert_problem(missing, 404)


async def test_invoice_provenance_requires_cursor_pair_and_auditor() -> None:
    reader = StubProvenanceReader()
    settings = ApiSettings(
        auditor_token=SecretStr("synthetic-auditor-token"),
        manager_token=SecretStr("synthetic-manager-token"),
    )
    async with client_for(create_app(settings, provenance_reader=reader)) as client:
        headers = {"Authorization": "Bearer synthetic-auditor-token", "X-Trace-ID": TRACE_ID}
        accepted = await client.get(
            f"/v1/invoices/{INVOICE_ID}/provenance",
            params={
                "limit": 2,
                "after_created_at": WHEN.isoformat(),
                "after_event_id": str(EVENT_ID),
            },
            headers=headers,
        )
        partial = await client.get(
            f"/v1/invoices/{INVOICE_ID}/provenance?after_event_id={EVENT_ID}",
            headers=headers,
        )
        naive = await client.get(
            f"/v1/invoices/{INVOICE_ID}/provenance?after_created_at=2026-09-24T12:00:00"
            f"&after_event_id={EVENT_ID}",
            headers=headers,
        )
        denied = await client.get(
            f"/v1/invoices/{INVOICE_ID}/provenance",
            headers={"Authorization": "Bearer synthetic-manager-token"},
        )
        missing = await client.get(f"/v1/invoices/{UUID(int=99)}/provenance", headers=headers)
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "NEEDS_REVIEW"
    assert reader.invoice_calls == [
        (INVOICE_ID, TRACE_ID, 2, WHEN, EVENT_ID),
        (UUID(int=99), TRACE_ID, 50, None, None),
    ]
    assert_problem(partial, 400)
    assert_problem(naive, 422)
    assert_problem(denied, 403)
    assert_problem(missing, 404)


async def test_reader_rejects_invalid_cursors_before_opening_the_database() -> None:
    reader = PostgresProvenanceReader(ApiSettings())
    with pytest.raises(InvalidProvenanceCursor):
        await reader.for_run_trace(RUN_ID, trace_id=TRACE_ID, after_sequence=0)
    with pytest.raises(InvalidProvenanceCursor):
        await reader.for_invoice(INVOICE_ID, trace_id=TRACE_ID, after_event_id=EVENT_ID)
    with pytest.raises(InvalidProvenanceCursor):
        await reader.for_invoice(
            INVOICE_ID,
            trace_id=TRACE_ID,
            after_created_at=datetime(2026, 9, 24, 12),
            after_event_id=EVENT_ID,
        )

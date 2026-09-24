"""Offline auditor authorization, pagination parameters, and problem responses."""

from uuid import UUID

import pytest
from pydantic import SecretStr
from tests.unit.test_api import assert_problem, client_for

from invoiceops_agent.api.app import create_app
from invoiceops_agent.api.audit_reader import AuditRunNotFound
from invoiceops_agent.api.schemas.audit import AuditRunPage
from invoiceops_agent.api.settings import ApiSettings

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
RUN_ID = UUID(int=71)
INVOICE_ID = UUID(int=72)
TRACE_ID = "a" * 32


class StubAuditReader:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, str, int, int | None]] = []

    async def for_run(
        self, run_id: UUID, *, trace_id: str, limit: int = 50, after_sequence: int | None = None
    ) -> AuditRunPage:
        self.calls.append((run_id, trace_id, limit, after_sequence))
        if run_id != RUN_ID:
            raise AuditRunNotFound("Run does not exist")
        return AuditRunPage(
            run_id=RUN_ID,
            invoice_id=INVOICE_ID,
            trace_id=TRACE_ID,
            status="PAUSED",
            events=[],
            next_cursor=None,
        )


async def test_auditor_only_ledger_page_passes_bounded_cursor() -> None:
    reader = StubAuditReader()
    settings = ApiSettings(
        auditor_token=SecretStr("synthetic-auditor-token"),
        analyst_token=SecretStr("synthetic-analyst-token"),
        service_token=SecretStr("synthetic-service-token"),
    )
    async with client_for(create_app(settings, audit_reader=reader)) as client:
        headers = {"Authorization": "Bearer synthetic-auditor-token", "X-Trace-ID": TRACE_ID}
        response = await client.get(
            f"/v1/runs/{RUN_ID}/ledger?limit=1&after_sequence=3", headers=headers
        )
        denied = await client.get(
            f"/v1/runs/{RUN_ID}/ledger",
            headers={"Authorization": "Bearer synthetic-analyst-token"},
        )
        service = await client.get(
            f"/v1/runs/{RUN_ID}/ledger",
            headers={"Authorization": "Bearer synthetic-service-token"},
        )
        invalid = await client.get(f"/v1/runs/{RUN_ID}/ledger?limit=201", headers=headers)
        missing = await client.get(f"/v1/runs/{UUID(int=99)}/ledger", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "PAUSED"
    assert reader.calls == [(RUN_ID, TRACE_ID, 1, 3), (UUID(int=99), TRACE_ID, 50, None)]
    assert_problem(denied, 403)
    assert_problem(service, 401)
    assert_problem(invalid, 422)
    assert_problem(missing, 404)

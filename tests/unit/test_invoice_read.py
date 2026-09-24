"""Offline role, queue contract, and deterministic priority tests."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError
from tests.unit.test_api import assert_problem, client_for

from invoiceops_agent.api.app import create_app
from invoiceops_agent.api.invoice_reader import InvalidInvoiceCursor, PostgresInvoiceReader
from invoiceops_agent.api.schemas.invoice_read import (
    InvoiceDetail,
    InvoiceListQuery,
    InvoicePage,
    InvoiceSummary,
)
from invoiceops_agent.api.settings import ApiSettings
from invoiceops_agent.schemas.exceptions import TaxonomyRequest
from invoiceops_agent.tools.exception_queue import project_exception
from invoiceops_agent.tools.exception_taxonomy import classify_exceptions

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
INVOICE_ID = UUID(int=1)
RUN_ID = UUID(int=2)
TOKENS = {
    "ANALYST": "synthetic-analyst-token",
    "MANAGER": "synthetic-manager-token",
    "AUDITOR": "synthetic-auditor-token",
}


class StubReader:
    def __init__(self) -> None:
        self.queries: list[InvoiceListQuery] = []
        self.ids: list[UUID] = []
        self.summary = InvoiceSummary(
            id=INVOICE_ID,
            run_id=RUN_ID,
            status="NEEDS_REVIEW",
            run_status="PAUSED",
            source="UPLOAD",
            content_type="application/pdf",
            created_at=NOW,
        )

    async def list(self, query: InvoiceListQuery) -> InvoicePage:
        self.queries.append(query)
        return InvoicePage(items=[self.summary])

    async def detail(self, invoice_id: UUID) -> InvoiceDetail:
        self.ids.append(invoice_id)
        return InvoiceDetail(invoice=self.summary, exception=None, evidence={}, read_at=NOW)


def _settings() -> ApiSettings:
    return ApiSettings(
        analyst_token=SecretStr(TOKENS["ANALYST"]),
        manager_token=SecretStr(TOKENS["MANAGER"]),
        auditor_token=SecretStr(TOKENS["AUDITOR"]),
    )


@pytest.mark.parametrize("role", ["ANALYST", "MANAGER"])
async def test_queue_roles_receive_bounded_filtered_page(role: str) -> None:
    reader = StubReader()
    async with client_for(create_app(_settings(), invoice_reader=reader)) as client:
        response = await client.get(
            "/v1/invoices?status=NEEDS_REVIEW&run_status=PAUSED&source=UPLOAD"
            "&exception_only=true&min_priority=2&limit=1",
            headers={"Authorization": f"Bearer {TOKENS[role]}"},
        )
    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == str(INVOICE_ID)
    assert reader.queries == [
        InvoiceListQuery(
            status="NEEDS_REVIEW",
            run_status="PAUSED",
            source="UPLOAD",
            exception_only=True,
            min_priority=2,
            limit=1,
        )
    ]


async def test_auditor_can_read_detail_but_not_operational_queue() -> None:
    reader = StubReader()
    headers = {"Authorization": f"Bearer {TOKENS['AUDITOR']}"}
    async with client_for(create_app(_settings(), invoice_reader=reader)) as client:
        assert_problem(await client.get("/v1/invoices", headers=headers), 403)
        response = await client.get(f"/v1/invoices/{INVOICE_ID}", headers=headers)
    assert response.status_code == 200
    assert reader.ids == [INVOICE_ID]
    assert reader.queries == []


async def test_read_auth_rejects_missing_wrong_duplicate_and_service_tokens() -> None:
    reader = StubReader()
    settings = _settings().model_copy(
        update={"service_token": SecretStr("synthetic-service-token")}
    )
    async with client_for(create_app(settings, invoice_reader=reader)) as client:
        for headers in (
            {},
            {"Authorization": "Bearer wrong"},
            {"Authorization": "Bearer synthetic-service-token"},
            [("Authorization", f"Bearer {TOKENS['ANALYST']}")] * 2,
        ):
            response = await client.get("/v1/invoices", headers=headers)
            assert_problem(response, 401)
            assert response.headers["www-authenticate"] == "Bearer"
    assert reader.queries == []


async def test_unconfigured_roles_and_invalid_query_return_problem_details() -> None:
    reader = StubReader()
    async with client_for(create_app(ApiSettings(), invoice_reader=reader)) as client:
        assert_problem(await client.get("/v1/invoices"), 503)
    async with client_for(create_app(_settings(), invoice_reader=reader)) as client:
        response = await client.get(
            "/v1/invoices?limit=101",
            headers={"Authorization": f"Bearer {TOKENS['ANALYST']}"},
        )
        assert_problem(response, 422)
    assert reader.queries == []


async def test_cursor_is_rejected_before_connecting() -> None:
    reader = PostgresInvoiceReader(_settings())
    for value in ("!", "bm90LWFjdXJzb3I", "MjAyNi0wOS0yNA=="):
        with pytest.raises(InvalidInvoiceCursor):
            await reader.list(InvoiceListQuery(cursor=value))


async def test_role_tokens_must_be_distinct() -> None:
    with pytest.raises(ValidationError, match="distinct"):
        ApiSettings(
            analyst_token=SecretStr(TOKENS["ANALYST"]),
            manager_token=SecretStr(TOKENS["ANALYST"]),
        )


async def test_queue_projection_is_reproducible_and_uses_versioned_sla() -> None:
    normal = project_exception(
        now=NOW,
        taxonomy=None,
        policy=None,
        gate=None,
        extraction_escalated=False,
        recommendation={"recommendation": "REVIEW"},
    )
    escalated = project_exception(
        now=NOW,
        taxonomy=None,
        policy=None,
        gate=None,
        extraction_escalated=True,
        recommendation={"recommendation": "REVIEW"},
    )
    taxonomy = classify_exceptions(
        TaxonomyRequest(
            run_id=RUN_ID, invoice_id=INVOICE_ID, trace_id="a" * 32, near_duplicate=True
        )
    )
    critical = project_exception(
        now=NOW,
        taxonomy=taxonomy,
        policy=None,
        gate=None,
        extraction_escalated=False,
        recommendation={"recommendation": "REVIEW"},
    )
    assert (normal.priority, normal.sla_due_at) == (1, NOW + timedelta(hours=72))
    assert (escalated.priority, escalated.sla_due_at) == (2, NOW + timedelta(hours=24))
    assert (critical.priority, critical.sla_due_at) == (3, NOW + timedelta(hours=4))
    assert critical.exception_type == "DUP_NEAR"
    assert critical == project_exception(
        now=NOW,
        taxonomy=taxonomy,
        policy=None,
        gate=None,
        extraction_escalated=False,
        recommendation={"recommendation": "REVIEW"},
    )

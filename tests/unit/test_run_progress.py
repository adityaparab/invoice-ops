"""Offline run-progress projection and protected API contract."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import SecretStr
from tests.unit.test_api import assert_problem, client_for

from invoiceops_agent.api.app import create_app
from invoiceops_agent.api.run_progress_reader import (
    RunNotFound,
    _active_node,
    _state,
)
from invoiceops_agent.api.schemas.run_progress import NodeProgress, RunProgress
from invoiceops_agent.api.settings import ApiSettings
from invoiceops_agent.graph.state import InvoiceNodeName

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
RUN_ID = UUID(int=51)
INVOICE_ID = UUID(int=52)
NOW = datetime(2026, 9, 24, tzinfo=UTC)


class StubProgressReader:
    def __init__(self) -> None:
        self.ids: list[UUID] = []

    async def read(self, run_id: UUID) -> RunProgress:
        self.ids.append(run_id)
        if run_id != RUN_ID:
            raise RunNotFound("Run does not exist")
        return RunProgress(
            run_id=RUN_ID,
            invoice_id=INVOICE_ID,
            status="RUNNING",
            graph_version="invoice-v1",
            active_node="Extract",
            nodes=[
                NodeProgress(name=name)
                for name in (
                    "Ingest",
                    "Extract",
                    "Validate",
                    "Match3Way",
                    "Policy",
                    "Gate",
                    "AutoApprove",
                    "ExceptionTriage",
                    "HumanReview",
                    "Archive",
                    "Reject",
                )
            ],
            started_at=NOW,
            completed_at=None,
            read_at=NOW,
        )


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("synthetic-analyst-token", 200),
        ("synthetic-manager-token", 200),
        ("synthetic-auditor-token", 200),
        ("synthetic-service-token", 200),
        ("incorrect-token", 401),
    ],
)
async def test_progress_roles_and_wire_contract(token: str, expected: int) -> None:
    reader = StubProgressReader()
    settings = ApiSettings(
        analyst_token=SecretStr("synthetic-analyst-token"),
        manager_token=SecretStr("synthetic-manager-token"),
        auditor_token=SecretStr("synthetic-auditor-token"),
        service_token=SecretStr("synthetic-service-token"),
    )
    async with client_for(create_app(settings, run_progress_reader=reader)) as client:
        response = await client.get(
            f"/v1/runs/{RUN_ID}/progress", headers={"Authorization": f"Bearer {token}"}
        )
    if expected == 200:
        assert response.status_code == 200
        assert response.json()["active_node"] == "Extract"
        assert response.json()["progress_source"] == "audit-ledger"
        assert reader.ids == [RUN_ID]
    else:
        assert_problem(response, expected)
        assert reader.ids == []


async def test_progress_missing_run_and_duplicate_auth_are_sanitized() -> None:
    reader = StubProgressReader()
    settings = ApiSettings(service_token=SecretStr("synthetic-service-token"))
    async with client_for(create_app(settings, run_progress_reader=reader)) as client:
        missing = await client.get(
            f"/v1/runs/{UUID(int=99)}/progress",
            headers={"Authorization": "Bearer synthetic-service-token"},
        )
        duplicate = await client.get(
            f"/v1/runs/{RUN_ID}/progress",
            headers=[("Authorization", "Bearer synthetic-service-token")] * 2,
        )
    assert_problem(missing, 404)
    assert_problem(duplicate, 401)
    assert reader.ids == [UUID(int=99)]


async def test_progress_projects_only_safe_node_fields_and_next_branch() -> None:
    extract = _state(
        "Extract",
        {
            "content_hash": "secret",
            "result": {
                "status": "EXTRACTED",
                "extraction": {
                    "vendor_name": {"value": "Synthetic Supplier", "confidence": "0.99"},
                    "bank_account_iban": {"value": "SECRET", "confidence": "0.99"},
                },
            },
        },
    )
    assert extract == {
        "status": "EXTRACTED",
        "fields": {"vendor_name": {"value": "Synthetic Supplier", "confidence": "0.99"}},
    }
    assert "SECRET" not in str(extract)
    observed: dict[InvoiceNodeName, NodeProgress] = {
        "Ingest": NodeProgress(name="Ingest", event_type="ingest.accepted"),
        "Extract": NodeProgress(name="Extract", event_type="extraction.escalated"),
    }
    assert _active_node("RUNNING", observed) == "ExceptionTriage"
    assert _active_node("PAUSED", observed) == "HumanReview"
    assert _active_node("FAILED", observed) is None

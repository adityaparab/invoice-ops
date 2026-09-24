"""Auditor and platform roles can read evaluation summaries, AP roles cannot."""

from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from tests.unit.test_api import assert_problem, client_for

from invoiceops_agent.api.app import create_app
from invoiceops_agent.api.schemas.evals import EvalDashboard
from invoiceops_agent.api.settings import ApiSettings

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class StubEvalReader:
    def __init__(self) -> None:
        self.calls = 0

    async def dashboard(self) -> EvalDashboard:
        self.calls += 1
        return EvalDashboard(reports=[], experiments=[], read_at=datetime(2026, 9, 24, tzinfo=UTC))


@pytest.mark.parametrize(
    ("token", "status"),
    [
        ("synthetic-auditor-token", 200),
        ("synthetic-service-token", 200),
        ("synthetic-analyst-token", 403),
        ("synthetic-manager-token", 403),
        ("incorrect-token", 401),
    ],
)
async def test_eval_report_roles(token: str, status: int) -> None:
    reader = StubEvalReader()
    settings = ApiSettings(
        auditor_token=SecretStr("synthetic-auditor-token"),
        service_token=SecretStr("synthetic-service-token"),
        analyst_token=SecretStr("synthetic-analyst-token"),
        manager_token=SecretStr("synthetic-manager-token"),
    )
    async with client_for(create_app(settings, eval_reader=reader)) as client:
        response = await client.get(
            "/v1/evals/reports", headers={"Authorization": f"Bearer {token}"}
        )
    if status == 200:
        assert response.status_code == 200
        assert response.json()["reports"] == []
        assert reader.calls == 1
    else:
        assert_problem(response, status)
        assert reader.calls == 0

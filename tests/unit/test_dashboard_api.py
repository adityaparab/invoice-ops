"""Role and parameter guards for the manager dashboard contract."""

from datetime import UTC, date, datetime

import pytest
from pydantic import SecretStr
from tests.unit.test_api import assert_problem, client_for

from invoiceops_agent.api.app import create_app
from invoiceops_agent.api.schemas.dashboard import AgingCounts, DailyVolume, DashboardSummary
from invoiceops_agent.api.settings import ApiSettings

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
TOKENS = {
    "manager": "synthetic-manager-dashboard-token",
    "analyst": "synthetic-analyst-dashboard-token",
    "auditor": "synthetic-auditor-dashboard-token",
}


class StubDashboard:
    def __init__(self) -> None:
        self.periods: list[int] = []

    async def summary(self, period_days: int) -> DashboardSummary:
        self.periods.append(period_days)
        return DashboardSummary(
            as_of=datetime(2026, 9, 24, tzinfo=UTC),
            period_days=period_days,
            invoice_count=0,
            resolved_count=0,
            auto_approved_count=0,
            stp_rate=None,
            open_exception_count=0,
            aging=AgingCounts(
                under_24_hours=0, one_to_three_days=0, over_three_days=0, sla_overdue=0
            ),
            cost_observed_invoices=0,
            total_observed_cost_usd=None,
            cost_per_observed_invoice_usd=None,
            cost_coverage="UNAVAILABLE",
            volume_by_day=(DailyVolume(day=date(2026, 9, 24), invoices=0),),
            exception_types=(),
        )


async def test_only_manager_reads_bounded_dashboard_period() -> None:
    dashboard = StubDashboard()
    settings = ApiSettings(
        manager_token=SecretStr(TOKENS["manager"]),
        analyst_token=SecretStr(TOKENS["analyst"]),
        auditor_token=SecretStr(TOKENS["auditor"]),
    )
    async with client_for(create_app(settings, dashboard_reader=dashboard)) as client:
        for role in ("analyst", "auditor"):
            assert_problem(
                await client.get(
                    "/v1/dashboard", headers={"Authorization": f"Bearer {TOKENS[role]}"}
                ),
                403,
            )
        assert_problem(await client.get("/v1/dashboard"), 401)
        assert_problem(
            await client.get(
                "/v1/dashboard?period_days=91",
                headers={"Authorization": f"Bearer {TOKENS['manager']}"},
            ),
            422,
        )
        response = await client.get(
            "/v1/dashboard?period_days=1",
            headers={"Authorization": f"Bearer {TOKENS['manager']}"},
        )
    assert response.status_code == 200
    assert response.json()["period_days"] == 1
    assert dashboard.periods == [1]

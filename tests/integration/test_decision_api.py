"""Two-person decisions are append-only, idempotent, and independently authenticated."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import SecretStr
from tests.integration.support import INVOICE_ID, RUN_ID
from tests.integration.test_ledger import ledger_runtime_dsn as ledger_runtime_dsn
from tests.integration.test_ledger import runtime_connection
from tests.unit.test_api import assert_problem, client_for

from invoiceops_agent.api.app import create_app
from invoiceops_agent.api.settings import ApiSettings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
EXCEPTION_ID = UUID(int=500)
TOKENS = {
    "analyst": "synthetic-analyst-token",
    "manager": "synthetic-manager-token",
    "auditor": "synthetic-auditor-token",
}
PROPOSAL = {
    "action": "APPROVE",
    "rationale": "Synthetic evidence matches",
    "reason_code": "MATCHED",
    "proposal_id": None,
}


def _headers(role: str, key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {TOKENS[role]}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


async def _seed_review(dsn: str) -> None:
    async with runtime_connection(dsn) as connection, connection.transaction():
        await connection.execute(
            "UPDATE public.runs SET status = 'PAUSED' WHERE id = %s", (RUN_ID,)
        )
        await connection.execute(
            "UPDATE public.invoices SET status = 'NEEDS_REVIEW' WHERE id = %s",
            (INVOICE_ID,),
        )
        await connection.execute(
            "INSERT INTO public.exceptions "
            "(id, run_id, invoice_id, exception_type, priority, sla_due_at, "
            "evidence, recommendation) "
            "VALUES (%s, %s, %s, 'PRICE_MM', 2, %s, '{}', '{}')",
            (EXCEPTION_ID, RUN_ID, INVOICE_ID, datetime(2026, 9, 25, tzinfo=UTC)),
        )


async def test_proposal_and_manager_signoff_enforce_four_eyes_and_replay(
    ledger_runtime_dsn: str,
) -> None:
    await _seed_review(ledger_runtime_dsn)
    app = create_app(
        ApiSettings(
            postgres_dsn=SecretStr(ledger_runtime_dsn),
            analyst_token=SecretStr(TOKENS["analyst"]),
            manager_token=SecretStr(TOKENS["manager"]),
            auditor_token=SecretStr(TOKENS["auditor"]),
        )
    )
    url = f"/v1/exceptions/{EXCEPTION_ID}/decision"
    async with client_for(app) as client:
        assert_problem(await client.post(url, json=PROPOSAL, headers=_headers("analyst")), 400)
        assert_problem(
            await client.post(url, json=PROPOSAL, headers=_headers("auditor", "auditor-1")),
            403,
        )
        first = await client.post(url, json=PROPOSAL, headers=_headers("analyst", "proposal-1"))
        assert first.status_code == 201
        proposal = first.json()
        assert proposal["stage"] == "PENDING_SIGNOFF"
        assert proposal["actor_id"] == "maria-ap-analyst"
        replay = await client.post(url, json=PROPOSAL, headers=_headers("analyst", "proposal-1"))
        assert replay.status_code == 201 and replay.json() == proposal
        assert_problem(
            await client.post(
                url,
                json={**PROPOSAL, "rationale": "Changed"},
                headers=_headers("analyst", "proposal-1"),
            ),
            409,
        )
        assert_problem(
            await client.post(url, json=PROPOSAL, headers=_headers("manager", "manager-1")),
            409,
        )
        signed = {
            **PROPOSAL,
            "rationale": "Independently checked",
            "proposal_id": proposal["decision_id"],
        }
        final = await client.post(url, json=signed, headers=_headers("manager", "manager-1"))
        assert final.status_code == 201
        accepted = final.json()
        assert accepted["stage"] == "QUEUED_FOR_RESUME"
        assert accepted["actor_id"] == "dan-procurement-manager"
        assert accepted["proposal_id"] == proposal["decision_id"]
        assert_problem(
            await client.post(url, json=signed, headers=_headers("manager", "manager-2")),
            409,
        )
    async with runtime_connection(ledger_runtime_dsn) as connection:
        rows = await (
            await connection.execute(
                "SELECT id, actor_id, supersedes_id FROM public.decisions "
                "WHERE exception_id = %s ORDER BY created_at, id",
                (EXCEPTION_ID,),
            )
        ).fetchall()
        events = await (
            await connection.execute(
                "SELECT event_type, actor_id, policy_version FROM public.ledger "
                "WHERE run_id = %s ORDER BY sequence",
                (RUN_ID,),
            )
        ).fetchall()
        status = await (
            await connection.execute(
                "SELECT status FROM public.exceptions WHERE id = %s", (EXCEPTION_ID,)
            )
        ).fetchone()
    assert len(rows) == 2
    assert rows[0]["id"] == UUID(proposal["decision_id"])
    assert rows[1]["supersedes_id"] == rows[0]["id"]
    assert [row["event_type"] for row in events] == ["decision.proposed", "decision.accepted"]
    assert all(row["policy_version"] == "four-eyes@v1" for row in events)
    assert status == {"status": "IN_REVIEW"}


async def test_manager_cannot_approve_a_nonapproval_proposal(ledger_runtime_dsn: str) -> None:
    await _seed_review(ledger_runtime_dsn)
    app = create_app(
        ApiSettings(
            postgres_dsn=SecretStr(ledger_runtime_dsn),
            analyst_token=SecretStr(TOKENS["analyst"]),
            manager_token=SecretStr(TOKENS["manager"]),
        )
    )
    url = f"/v1/exceptions/{EXCEPTION_ID}/decision"
    async with client_for(app) as client:
        proposal = await client.post(
            url,
            json={**PROPOSAL, "action": "RETURN"},
            headers=_headers("analyst", "return-proposal"),
        )
        assert proposal.status_code == 201
        assert_problem(
            await client.post(
                url,
                json={**PROPOSAL, "proposal_id": proposal.json()["decision_id"]},
                headers=_headers("manager", "invalid-approval"),
            ),
            409,
        )

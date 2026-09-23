"""Typed ERP reads and three-way decisions use restricted Postgres and the audit ledger."""

import psycopg
import pytest
from psycopg.rows import dict_row
from pydantic import SecretStr
from sqlalchemy.engine import make_url
from tests.integration.conftest import TEST_PASSWORD
from tests.integration.support import INVOICE_ID, RUN_ID
from tests.integration.test_ledger import ledger_runtime_dsn as ledger_runtime_dsn
from tests.integration.test_ledger import runtime_connection, writer
from tests.unit.matching_support import matching_request, snapshot_for

from invoiceops_agent.db.runtime_role import provision_runtime_login
from invoiceops_agent.db.settings import ProvisioningSettings
from invoiceops_agent.graph.nodes.match3way import Match3WayNode
from invoiceops_agent.ledger.audit import TransactionalAuditSink
from invoiceops_agent.ledger.reader import LedgerReader
from invoiceops_agent.schemas.matching import MatchResult
from invoiceops_agent.tools.erp_generator import generate_fixture
from invoiceops_agent.tools.erp_repository import ERPDataIntegrityError, ERPRepository
from invoiceops_agent.tools.erp_seed import seed_fixture
from invoiceops_agent.tools.matching import match_invoice

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_restricted_erp_reader_loads_seeded_snapshot_and_rejects_bad_lines(
    migrated_database: psycopg.Connection[tuple[object, ...]], migration_dsn: str
) -> None:
    provision_runtime_login(
        ProvisioningSettings(
            migration_dsn=SecretStr(migration_dsn), app_password=SecretStr(TEST_PASSWORD)
        )
    )
    runtime_dsn = (
        make_url(migration_dsn)
        .set(drivername="postgresql", username="invoiceops_app", password=TEST_PASSWORD)
        .render_as_string(hide_password=False)
    )
    fixture = generate_fixture()
    expected = snapshot_for(fixture=fixture)
    async with await psycopg.AsyncConnection.connect(
        runtime_dsn, row_factory=dict_row, autocommit=True
    ) as connection:
        async with connection.transaction():
            assert await seed_fixture(connection, fixture) == "created"
        loaded = await ERPRepository.snapshot(connection, expected.purchase_order.po_number)
        assert loaded == expected
        assert loaded is not None
        assert match_invoice(matching_request(loaded)).status == "PASS"
        assert await ERPRepository.snapshot(connection, "SYN-ABSENT") is None
        await connection.execute(
            "UPDATE public.purchase_orders SET lines = '[]'::jsonb WHERE id = %s",
            (expected.purchase_order.id,),
        )
        with pytest.raises(ERPDataIntegrityError, match="matching contract"):
            await ERPRepository.snapshot(connection, expected.purchase_order.po_number)


async def test_match_node_commits_comparison_evidence_before_return(
    ledger_runtime_dsn: str,
) -> None:
    sink = TransactionalAuditSink(lambda: runtime_connection(ledger_runtime_dsn), writer())
    request = matching_request(snapshot_for()).model_copy(
        update={"run_id": RUN_ID, "invoice_id": INVOICE_ID, "trace_id": "d" * 32}
    )
    result = await Match3WayNode(sink).run(request)
    async with runtime_connection(ledger_runtime_dsn) as connection:
        page = await LedgerReader().for_run(connection, RUN_ID, trace_id="d" * 32)
    assert len(page.events) == 1
    event = page.events[0]
    assert event.event_type == "matching.completed"
    assert event.actor_type == "POLICY"
    assert event.versions.policy_version == "three-way-match@v1"
    assert MatchResult.model_validate(event.payload) == result
    assert result.status == "PASS"

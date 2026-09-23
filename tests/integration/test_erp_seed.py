"""The restricted runtime role seeds the ERP once and detects changed rows."""

import psycopg
import pytest
from psycopg.rows import dict_row
from pydantic import SecretStr
from sqlalchemy.engine import make_url
from tests.integration.conftest import TEST_PASSWORD

from invoiceops_agent.db.runtime_role import provision_runtime_login
from invoiceops_agent.db.settings import ProvisioningSettings
from invoiceops_agent.tools.erp_generator import generate_fixture
from invoiceops_agent.tools.erp_seed import ERPSeedConflict, seed_fixture

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_seed_is_atomic_repeatable_and_detects_drift(
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
    async with await psycopg.AsyncConnection.connect(
        runtime_dsn, row_factory=dict_row, autocommit=True
    ) as connection:
        async with connection.transaction():
            assert await seed_fixture(connection, fixture) == "created"
        async with connection.transaction():
            assert await seed_fixture(connection, fixture) == "unchanged"
        for table, count in (("vendors", 12), ("purchase_orders", 24), ("goods_receipts", 12)):
            cursor = await connection.execute(f"SELECT count(*) AS count FROM public.{table}")
            row = await cursor.fetchone()
            assert row is not None and row["count"] == count
        await connection.execute(
            "UPDATE public.vendors SET name = 'drifted' WHERE id = %s",
            (fixture.vendors[0].id,),
        )
        async with connection.transaction():
            with pytest.raises(ERPSeedConflict, match="differs"):
                await seed_fixture(connection, fixture)

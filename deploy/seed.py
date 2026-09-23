"""One-shot Compose entry point for the seed-pinned synthetic ERP fixture."""

import asyncio
import logging
from time import perf_counter

import psycopg
from psycopg.rows import dict_row
from pydantic import ValidationError

from invoiceops_agent.tools.erp_generator import fixture_sha256, generate_fixture
from invoiceops_agent.tools.erp_seed import ERPSeedConflict, seed_fixture
from invoiceops_agent.tools.erp_seed_settings import ERPSeedSettings

logger = logging.getLogger(__name__)


async def _seed() -> None:
    settings = ERPSeedSettings()
    fixture = generate_fixture(settings.seed)
    started = perf_counter()
    async with await psycopg.AsyncConnection.connect(
        settings.postgres_dsn.get_secret_value(),
        row_factory=dict_row,
        autocommit=True,
        connect_timeout=5,
        options=" ".join(
            (
                "-c timezone=UTC",
                "-c search_path=public",
                "-c statement_timeout=60000",
                "-c lock_timeout=5000",
            )
        ),
    ) as connection:
        async with connection.transaction():
            outcome = await seed_fixture(connection, fixture)
    logger.info(
        "erp_seed_completed version=%s seed=%d outcome=%s vendors=%d orders=%d receipts=%d "
        "fixture_sha256=%s duration_ms=%.3f",
        fixture.version,
        fixture.seed,
        outcome,
        len(fixture.vendors),
        len(fixture.purchase_orders),
        len(fixture.goods_receipts),
        fixture_sha256(fixture),
        (perf_counter() - started) * 1000,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        asyncio.run(_seed())
    except (ValidationError, ERPSeedConflict, psycopg.Error, OSError, ValueError) as error:
        logger.error("erp_seed_failed error_type=%s", type(error).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

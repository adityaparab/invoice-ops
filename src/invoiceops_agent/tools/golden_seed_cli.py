"""Seed the committed golden ERP snapshot into a Compose evaluation database."""

import argparse
import asyncio
import hashlib
import logging
from pathlib import Path
from time import perf_counter

import psycopg
from psycopg.rows import dict_row
from pydantic import ValidationError

from invoiceops_agent.schemas.erp import GoldenERPSeed
from invoiceops_agent.tools.erp_seed import ERPSeedConflict, seed_golden_fixture
from invoiceops_agent.tools.erp_seed_settings import ERPSeedSettings

logger = logging.getLogger(__name__)


async def seed(path: Path) -> None:
    settings = ERPSeedSettings()
    fixture = GoldenERPSeed.model_validate_json(await asyncio.to_thread(path.read_bytes))
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
            outcome = await seed_golden_fixture(connection, fixture)
    logger.info(
        "golden_erp_seed_completed version=%s seed=%d outcome=%s vendors=%d orders=%d "
        "receipts=%d fixture_sha256=%s duration_ms=%.3f",
        fixture.version,
        fixture.seed,
        outcome,
        len(fixture.vendors),
        len(fixture.purchase_orders),
        len(fixture.goods_receipts),
        hashlib.sha256(fixture.model_dump_json().encode()).hexdigest(),
        (perf_counter() - started) * 1000,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=Path("eval/golden/v1.0.1/erp.json"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        asyncio.run(seed(args.fixture))
    except (ValidationError, ERPSeedConflict, psycopg.Error, OSError, ValueError) as error:
        logger.error("golden_erp_seed_failed error_type=%s", type(error).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

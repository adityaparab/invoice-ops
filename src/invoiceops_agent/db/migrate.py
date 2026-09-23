"""One-shot owner process: migrate first, then provision the restricted runtime login."""

import logging
from time import perf_counter

import psycopg
from alembic import command
from alembic.config import Config
from alembic.util import CommandError
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from invoiceops_agent.db.runtime_role import RuntimeRoleError, provision_runtime_login
from invoiceops_agent.db.settings import ProvisioningSettings
from invoiceops_agent.obs.logging import configure_logging

logger = logging.getLogger(__name__)


def main() -> int:
    """Exit nonzero on bootstrap failure without logging DSNs, passwords, or SQL parameters."""
    configure_logging()
    started = perf_counter()
    try:
        settings = ProvisioningSettings()
        config = Config("alembic.ini")
        config.attributes["configure_logger"] = False
        command.upgrade(config, "head")
        provision_runtime_login(settings)
    except (
        ValidationError,
        RuntimeRoleError,
        CommandError,
        SQLAlchemyError,
        psycopg.Error,
    ) as error:
        logger.error(
            "event=database.bootstrap.failed error_type=%s duration_ms=%.1f",
            type(error).__name__,
            (perf_counter() - started) * 1000,
        )
        return 1
    logger.info(
        "event=database.bootstrap.completed target=head duration_ms=%.1f",
        (perf_counter() - started) * 1000,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

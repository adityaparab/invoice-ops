"""Shared bounded async connection for operational read projections."""

import psycopg
from psycopg.rows import dict_row

from invoiceops_agent.api.settings import ApiSettings


class ReadStoreUnavailable(Exception):
    """The restricted operational read connection is unavailable."""


async def connect_read_store(settings: ApiSettings) -> psycopg.AsyncConnection[dict[str, object]]:
    if settings.postgres_dsn is None:
        raise ReadStoreUnavailable("Operational reads are not configured")
    try:
        return await psycopg.AsyncConnection.connect(
            settings.postgres_dsn.get_secret_value(),
            autocommit=True,
            row_factory=dict_row,
            connect_timeout=5,
            options="-c statement_timeout=10000 -c lock_timeout=10000",
        )
    except psycopg.Error as error:
        raise ReadStoreUnavailable("Operational read connection failed") from error

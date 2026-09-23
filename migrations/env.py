"""Run schema migrations through an explicitly configured owner connection."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from invoiceops_agent.db.settings import (
    OWNER_CONNECT_TIMEOUT,
    OWNER_CONNECTION_OPTIONS,
    MigrationSettings,
)

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def run_migrations_offline() -> None:
    settings = MigrationSettings()
    context.configure(
        url=settings.migration_dsn.get_secret_value(),
        target_metadata=None,
        version_table_schema="public",
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    settings = MigrationSettings()
    engine = create_engine(
        settings.migration_dsn.get_secret_value(),
        poolclass=pool.NullPool,
        hide_parameters=True,
        connect_args={
            "connect_timeout": OWNER_CONNECT_TIMEOUT,
            "options": OWNER_CONNECTION_OPTIONS,
        },
    )
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection, target_metadata=None, version_table_schema="public"
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

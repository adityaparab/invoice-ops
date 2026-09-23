"""Run schema migrations through an explicitly configured owner connection."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from invoiceops_agent.db.settings import MigrationSettings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


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
            "connect_timeout": 5,
            "options": (
                "-c timezone=UTC -c search_path=public "
                "-c lock_timeout=5000 -c statement_timeout=60000"
            ),
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

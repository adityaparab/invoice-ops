"""Owner connection configuration for the migration CLI."""

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class MigrationSettings(BaseSettings):
    """Require a separate owner DSN; never fall back to the runtime connection."""

    model_config = SettingsConfigDict(
        env_prefix="INVOICEOPS_", extra="ignore", hide_input_in_errors=True
    )

    migration_dsn: SecretStr

    @field_validator("migration_dsn")
    @classmethod
    def validate_postgres_owner_dsn(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
        except ArgumentError:
            raise ValueError("Migration DSN must be a PostgreSQL psycopg URL") from None
        if url.drivername != "postgresql+psycopg" or not url.database or not url.username:
            raise ValueError("Migration DSN must select a database and user via postgresql+psycopg")
        return value

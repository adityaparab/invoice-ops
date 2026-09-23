"""Owner connection configuration for the migration CLI."""

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

OWNER_CONNECT_TIMEOUT = 5
OWNER_CONNECTION_OPTIONS = (
    "-c timezone=UTC -c search_path=public -c lock_timeout=5000 -c statement_timeout=60000"
)


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


class ProvisioningSettings(MigrationSettings):
    """Runtime credentials are required only by the separate deployment bootstrap CLI."""

    app_password: SecretStr

    @field_validator("app_password")
    @classmethod
    def validate_app_password(cls, value: SecretStr) -> SecretStr:
        password = value.get_secret_value()
        if not password.strip() or "\x00" in password:
            raise ValueError("Application password must be nonempty and contain no NUL character")
        return value

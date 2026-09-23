"""Environment-backed connection and seed selection for the one-shot ERP loader."""

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from invoiceops_agent.tools.erp_generator import DEFAULT_SEED


class ERPSeedSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INVOICEOPS_", extra="ignore", hide_input_in_errors=True
    )

    postgres_dsn: SecretStr
    seed: int = Field(default=DEFAULT_SEED, ge=0, le=2**32 - 1)

    @field_validator("postgres_dsn")
    @classmethod
    def validate_dsn(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
        except ArgumentError:
            raise ValueError("ERP seed DSN must be a PostgreSQL URL") from None
        if url.drivername != "postgresql" or not url.database or not url.username:
            raise ValueError("ERP seed DSN must select a PostgreSQL database and user")
        return value

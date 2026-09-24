"""Graph configuration independent from the HTTP transport."""

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class GraphSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INVOICEOPS_", extra="ignore", hide_input_in_errors=True
    )

    checkpoint_dsn: SecretStr
    graph_timeout_seconds: float = Field(default=30, gt=0, le=300)
    invoice_graph_timeout_seconds: float = Field(default=420, gt=0, le=450)

    @field_validator("checkpoint_dsn")
    @classmethod
    def validate_checkpoint_dsn(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith(("postgresql://", "postgres://")):
            raise ValueError("Checkpoint DSN must use a PostgreSQL URI scheme")
        return value

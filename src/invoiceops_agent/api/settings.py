"""Environment-backed API configuration, loaded only when the app is created."""

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseSettings):
    """Optional infrastructure configuration keeps liveness independent of availability."""

    model_config = SettingsConfigDict(
        env_prefix="INVOICEOPS_", extra="ignore", hide_input_in_errors=True
    )

    postgres_dsn: SecretStr | None = None
    minio_url: HttpUrl | None = None
    readiness_timeout_seconds: float = Field(default=2.0, gt=0, le=30)

    @field_validator("postgres_dsn")
    @classmethod
    def validate_postgres_dsn(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().startswith(
            ("postgresql://", "postgres://")
        ):
            raise ValueError("Postgres DSN must use the postgresql:// or postgres:// scheme")
        return value

    @field_validator("minio_url")
    @classmethod
    def validate_minio_url(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None and (
            value.username or value.password or value.query or value.fragment or value.path != "/"
        ):
            raise ValueError("MinIO URL must be an HTTP origin without credentials, path, or query")
        return value

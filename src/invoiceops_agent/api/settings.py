"""Environment-backed API configuration, loaded only when the app is created."""

from typing import Self

from pydantic import Field, SecretStr, field_validator, model_validator

from invoiceops_agent.tools.storage_settings import StorageSettings


class ApiSettings(StorageSettings):
    """Optional infrastructure configuration keeps liveness independent of availability."""

    postgres_dsn: SecretStr | None = None
    readiness_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    service_token: SecretStr | None = None
    document_max_bytes: int = Field(default=10 * 1024 * 1024, gt=0, le=100 * 1024 * 1024)
    upload_max_bytes: int = Field(default=11 * 1024 * 1024, gt=0, le=101 * 1024 * 1024)
    upload_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    webhook_secret: SecretStr | None = None
    webhook_window_seconds: int = Field(default=300, ge=1, le=3600)
    webhook_max_bytes: int = Field(default=14 * 1024 * 1024, gt=0, le=140 * 1024 * 1024)
    webhook_timeout_seconds: float = Field(default=30.0, gt=0, le=120)

    @field_validator("postgres_dsn")
    @classmethod
    def validate_postgres_dsn(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().startswith(
            ("postgresql://", "postgres://")
        ):
            raise ValueError("Postgres DSN must use the postgresql:// or postgres:// scheme")
        return value

    @field_validator("service_token")
    @classmethod
    def validate_service_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and (
            len(value.get_secret_value()) < 16
            or any(not 0x21 <= ord(character) <= 0x7E for character in value.get_secret_value())
        ):
            raise ValueError(
                "Service token must contain at least 16 printable ASCII characters without spaces"
            )
        return value

    @model_validator(mode="after")
    def validate_upload_limits(self) -> Self:
        if self.upload_max_bytes <= self.document_max_bytes:
            raise ValueError("Whole upload limit must exceed the document limit")
        return self

    @field_validator("webhook_secret")
    @classmethod
    def validate_webhook_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and (
            len(value.get_secret_value()) < 32
            or any(not 0x21 <= ord(character) <= 0x7E for character in value.get_secret_value())
        ):
            raise ValueError("Webhook secret must have at least 32 printable ASCII characters")
        return value

"""Environment-backed raw-object storage configuration shared by API and bootstrap CLI."""

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class StorageSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INVOICEOPS_", extra="ignore", hide_input_in_errors=True
    )

    minio_url: HttpUrl | None = None
    minio_access_key: SecretStr | None = None
    minio_secret_key: SecretStr | None = None
    raw_bucket: str = Field(default="invoiceops-raw", pattern=r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
    storage_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    @field_validator("minio_url")
    @classmethod
    def validate_minio_url(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None and (
            value.username or value.password or value.query or value.fragment or value.path != "/"
        ):
            raise ValueError("MinIO URL must be an HTTP origin without credentials, path, or query")
        return value

    @field_validator("minio_access_key", "minio_secret_key")
    @classmethod
    def validate_storage_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().strip():
            raise ValueError("Storage credentials must be nonblank")
        return value

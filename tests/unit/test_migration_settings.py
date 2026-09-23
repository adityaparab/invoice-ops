"""The migration CLI requires an explicit owner DSN and redacts its credentials."""

import pytest
from pydantic import SecretStr, ValidationError

from invoiceops_agent.db.settings import MigrationSettings

pytestmark = pytest.mark.unit


def test_owner_dsn_is_read_from_environment_and_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    dsn = "postgresql+psycopg://owner:synthetic-password@localhost/invoiceops_test"
    monkeypatch.setenv("INVOICEOPS_MIGRATION_DSN", dsn)

    settings = MigrationSettings()

    assert settings.migration_dsn.get_secret_value() == dsn
    assert "synthetic-password" not in repr(settings)


def test_migrations_never_fall_back_to_application_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("INVOICEOPS_MIGRATION_DSN", raising=False)
    monkeypatch.setenv(
        "INVOICEOPS_DATABASE_DSN",
        "postgresql+psycopg://application:synthetic-password@localhost/invoiceops_test",
    )

    with pytest.raises(ValidationError, match="migration_dsn"):
        MigrationSettings()


@pytest.mark.parametrize(
    "dsn",
    [
        "invalid-synthetic-password",
        "sqlite:///synthetic-password.db",
        "postgresql://owner:synthetic-password@localhost/invoiceops_test",
        "postgresql+asyncpg://owner:synthetic-password@localhost/invoiceops_test",
        "postgresql+psycopg://owner:synthetic-password@localhost",
        "postgresql+psycopg:///synthetic-password",
    ],
)
def test_invalid_owner_dsn_is_rejected_without_exposing_input(dsn: str) -> None:
    with pytest.raises(ValidationError) as failure:
        MigrationSettings(migration_dsn=SecretStr(dsn))

    assert "synthetic-password" not in str(failure.value)

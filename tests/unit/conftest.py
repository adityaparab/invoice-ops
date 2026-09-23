"""Keep unit configuration independent from the developer's environment."""

import pytest


@pytest.fixture(autouse=True)
def clear_api_config_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("POSTGRES_DSN", "MINIO_URL", "READINESS_TIMEOUT_SECONDS"):
        monkeypatch.delenv(f"INVOICEOPS_{name}", raising=False)

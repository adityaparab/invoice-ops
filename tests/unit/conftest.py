"""Keep unit configuration independent from the developer's environment."""

import pytest

from invoiceops_agent.api.settings import ApiSettings


@pytest.fixture(autouse=True)
def clear_api_config_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ApiSettings.model_fields:
        monkeypatch.delenv(f"INVOICEOPS_{name.upper()}", raising=False)

"""Require explicit environment-backed provenance; missing pins never get implicit defaults."""

from pydantic_settings import BaseSettings, SettingsConfigDict

from invoiceops_agent.ledger.schemas import VersionOverrides, VersionPins


class LedgerSettings(VersionPins, BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INVOICEOPS_LEDGER_", extra="ignore", hide_input_in_errors=True
    )

    def resolve(self, overrides: VersionOverrides | None = None) -> VersionPins:
        values = self.model_dump()
        if overrides is not None:
            values.update(overrides.model_dump(exclude_none=True))
        return VersionPins.model_validate(values)

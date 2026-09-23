"""Use only the caller's LiteLLM URL, key, and embedding-model name."""

from pydantic import HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from invoiceops_agent.gateway_client.schemas import Version
from invoiceops_agent.gateway_client.settings import AliasPolicy, GatewaySettings


class LiteLLMEmbeddingSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LITELLM_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    api_base: HttpUrl
    master_key: SecretStr
    embed_model: Version

    @field_validator("embed_model")
    @classmethod
    def reject_legacy_pin(cls, value: str) -> str:
        if value == "__legacy_unpinned__":
            raise ValueError("Legacy vectors cannot be an embedding model route")
        return value

    def gateway_settings(self) -> GatewaySettings:
        return GatewaySettings(
            base_url=self.api_base,
            api_key=self.master_key,
            aliases={
                "embed": AliasPolicy(
                    model_version=self.embed_model,
                    model_name=self.embed_model,
                )
            },
            _env_file=None,
        )

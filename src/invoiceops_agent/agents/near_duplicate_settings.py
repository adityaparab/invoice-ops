"""Use only the caller's LiteLLM URL, key, and embedding-model name."""

from pydantic import HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from invoiceops_agent.gateway_client.schemas import Version
from invoiceops_agent.gateway_client.settings import AliasPolicy, GatewaySettings


class LiteLLMEmbeddingSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LITELLM_",
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
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
        )


class LiteLLMWorkflowSettings(LiteLLMEmbeddingSettings):
    """Add the existing general/vision names without reading proxy config files."""

    model: Version
    extract_model: Version | None = None
    triage_model: Version | None = None

    def gateway_settings(self) -> GatewaySettings:
        vision_model = self.extract_model or self.model
        return GatewaySettings(
            base_url=self.api_base,
            api_key=self.master_key,
            aliases={
                "extract-vision": AliasPolicy(
                    model_version=vision_model,
                    model_name=vision_model,
                    allow_images=True,
                    allow_pdf=True,
                ),
                "embed": AliasPolicy(
                    model_version=self.embed_model,
                    model_name=self.embed_model,
                ),
                "triage-reasoner": AliasPolicy(
                    model_version=self.triage_model or self.model,
                    model_name=self.triage_model or self.model,
                ),
            },
        )

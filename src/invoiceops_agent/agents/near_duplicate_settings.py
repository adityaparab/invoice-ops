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
    embed_public_model: Version | None = None
    embed_fallback_model: Version | None = None
    embed_public_fallback_model: Version | None = None

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
                    public_model_name=self.embed_public_model,
                    fallback_model_name=self.embed_fallback_model,
                    public_fallback_model_name=self.embed_public_fallback_model,
                )
            },
        )


class LiteLLMWorkflowSettings(LiteLLMEmbeddingSettings):
    """Add the existing general/vision names without reading proxy config files."""

    model: Version
    extract_model: Version | None = None
    triage_model: Version | None = None
    extract_public_model: Version | None = None
    extract_fallback_model: Version | None = None
    extract_public_fallback_model: Version | None = None
    triage_public_model: Version | None = None
    triage_fallback_model: Version | None = None
    triage_public_fallback_model: Version | None = None
    adk_model: Version | None = None

    def adk_gateway_settings(self) -> GatewaySettings:
        """Route ADK variant intelligence to a named Gemini alias on the same proxy."""
        if self.adk_model is None:
            raise ValueError("LITELLM_ADK_MODEL is required for the ADK workflow")
        settings = self.gateway_settings()
        aliases = dict(settings.aliases)
        for alias in ("extract-vision", "triage-reasoner"):
            aliases[alias] = aliases[alias].model_copy(
                update={
                    "model_name": self.adk_model,
                    "model_version": self.adk_model,
                    "public_model_name": None,
                    "fallback_model_name": None,
                    "public_fallback_model_name": None,
                }
            )
        return settings.model_copy(update={"aliases": aliases})

    def gateway_settings(self) -> GatewaySettings:
        vision_model = self.extract_model or self.model
        return GatewaySettings(
            base_url=self.api_base,
            api_key=self.master_key,
            aliases={
                "extract-vision": AliasPolicy(
                    model_version=vision_model,
                    model_name=vision_model,
                    public_model_name=self.extract_public_model,
                    fallback_model_name=self.extract_fallback_model,
                    public_fallback_model_name=self.extract_public_fallback_model,
                    allow_images=True,
                    allow_pdf=True,
                    response_format="json_object",
                ),
                "embed": AliasPolicy(
                    model_version=self.embed_model,
                    model_name=self.embed_model,
                    public_model_name=self.embed_public_model,
                    fallback_model_name=self.embed_fallback_model,
                    public_fallback_model_name=self.embed_public_fallback_model,
                ),
                "triage-reasoner": AliasPolicy(
                    model_version=self.triage_model or self.model,
                    model_name=self.triage_model or self.model,
                    public_model_name=self.triage_public_model,
                    fallback_model_name=self.triage_fallback_model,
                    public_fallback_model_name=self.triage_public_fallback_model,
                    response_format="json_object",
                ),
            },
        )

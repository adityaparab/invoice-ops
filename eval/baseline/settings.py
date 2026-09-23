"""The baseline's only LiteLLM inputs are the URL, key, and model name."""

from pydantic import HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from invoiceops_agent.gateway_client import GatewaySettings
from invoiceops_agent.gateway_client.schemas import Version


class BaselineSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LITELLM_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    api_base: HttpUrl
    master_key: SecretStr
    model: Version
    extract_model: Version | None = None

    def gateway_settings(self) -> GatewaySettings:
        return GatewaySettings.model_validate(
            {
                "base_url": self.api_base,
                "api_key": self.master_key,
                "aliases": {
                    "extract-vision": {
                        "model_version": self.extract_model or self.model,
                        "model_name": self.extract_model or self.model,
                        "allow_images": True,
                        "response_format": "json_schema",
                    }
                },
            }
        )

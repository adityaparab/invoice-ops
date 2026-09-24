"""Direct LiteLLM endpoint and explicit model name for the eval-only judge."""

from pydantic import HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from invoiceops_agent.gateway_client.schemas import Version
from invoiceops_agent.gateway_client.settings import AliasPolicy, GatewaySettings


class LiteLLMJudgeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LITELLM_",
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    api_base: HttpUrl
    master_key: SecretStr
    judge_model: Version

    def gateway_settings(self) -> GatewaySettings:
        return GatewaySettings(
            base_url=self.api_base,
            api_key=self.master_key,
            aliases={
                "eval-judge": AliasPolicy(
                    model_version=self.judge_model,
                    model_name=self.judge_model,
                )
            },
        )

"""Environment-backed gateway and alias policy; no provider fallback endpoint."""

import re
from decimal import Decimal
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    field_validator,
    model_validator,
)

from invoiceops_agent.gateway_client.schemas import Contract, DataSensitivity, ModelAlias, Version


class AliasPolicy(Contract):
    model_version: Version
    model_name: Version | None = None
    public_model_name: Version | None = None
    fallback_model_name: Version | None = None
    public_fallback_model_name: Version | None = None
    input_token_limit: int = Field(default=32_768, gt=0)
    output_token_limit: int = Field(default=4096, gt=0)
    total_token_limit: int = Field(default=36_864, gt=0)
    binary_token_reserve: int = Field(default=8192, gt=0)
    allow_images: bool = False
    allow_pdf: bool = False
    response_format: Literal["text", "json_object", "json_schema"] = "text"

    @model_validator(mode="after")
    def validate_limits(self) -> Self:
        if self.output_token_limit >= self.total_token_limit:
            raise ValueError("Output limit must leave room for input tokens")
        return self

    def routes_for(
        self, sensitivity: DataSensitivity, alias: ModelAlias
    ) -> tuple[tuple[str, str], ...]:
        primary = (
            self.public_model_name
            if sensitivity == "public" and self.public_model_name
            else self.model_name
        ) or alias
        primary_version = (
            self.public_model_name
            if sensitivity == "public" and self.public_model_name
            else self.model_version
        )
        fallback = (
            self.public_fallback_model_name or self.fallback_model_name
            if sensitivity == "public"
            else self.fallback_model_name
        )
        routes = [(primary, primary_version)]
        if fallback is not None and fallback != primary:
            routes.append((fallback, fallback))
        return tuple(routes)


class GatewaySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, frozen=True)

    base_url: HttpUrl
    api_key: SecretStr
    aliases: dict[ModelAlias, AliasPolicy] = Field(min_length=1)
    request_timeout_seconds: float = Field(default=30, gt=0, le=300, allow_inf_nan=False)
    total_timeout_seconds: float = Field(default=60, gt=0, le=600, allow_inf_nan=False)
    max_attempts: int = Field(default=3, ge=1, le=6)
    backoff_seconds: float = Field(default=0.5, ge=0, le=30, allow_inf_nan=False)
    max_retry_delay_seconds: float = Field(default=10, gt=0, le=120, allow_inf_nan=False)
    max_binary_bytes: int = Field(default=5_000_000, gt=0, le=10_000_000)
    max_binary_parts: int = Field(default=4, gt=0, le=16)
    max_request_bytes: int = Field(default=14_000_000, gt=0, le=56_000_000)
    budget_alert_usd: Decimal = Field(default=Decimal("0.04"), gt=0, allow_inf_nan=False)
    semantic_cache_min_similarity: float = Field(default=0.995, gt=0, le=1)
    semantic_cache_ttl_seconds: int = Field(default=86_400, gt=0, le=604_800)
    semantic_cache_probe_timeout_seconds: float = Field(default=10, gt=0, le=30)
    semantic_cache_store_timeout_seconds: float = Field(default=5, gt=0, le=30)
    pii_patterns: dict[str, str] = Field(
        default_factory=lambda: {
            "EMAIL": r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b",
            "IBAN": r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b",
            "PHONE": r"(?<!\w)\+\d[\d ()\-]{7,18}\d\b",
        }
    )
    injection_patterns: tuple[str, ...] = (
        r"\bignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions\b",
        r"\b(?:reveal|print|disclose)\s+(?:the\s+)?system\s+prompt\b",
    )

    @field_validator("base_url")
    @classmethod
    def validate_gateway_url(cls, value: HttpUrl) -> HttpUrl:
        if value.username or value.password or value.query or value.fragment:
            raise ValueError("Gateway URL cannot contain credentials, query, or fragment")
        if value.host in {
            "api.openai.com",
            "api.anthropic.com",
            "generativelanguage.googleapis.com",
        }:
            raise ValueError("Configure the LiteLLM gateway, not a provider endpoint")
        if (value.path or "").rstrip("/") != "/v1":
            raise ValueError("Gateway URL must include the /v1 API prefix")
        return value

    @field_validator("api_key")
    @classmethod
    def validate_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("Gateway API key cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_patterns(self) -> Self:
        if any(not re.fullmatch(r"[A-Z_]{1,32}", name) for name in self.pii_patterns):
            raise ValueError("PII rule names must be uppercase identifiers")
        for pattern in (*self.pii_patterns.values(), *self.injection_patterns):
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error:
                raise ValueError("Guardrail pattern is not a valid regular expression") from None
        return self

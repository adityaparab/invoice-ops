"""Typed model-doorway contracts; document contents are excluded from repr output."""

import base64
import binascii
from decimal import Decimal
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ModelAlias = Literal["extract-vision", "triage-reasoner", "eval-judge", "embed"]
ChatAlias = Literal["extract-vision", "triage-reasoner", "eval-judge"]
DataSensitivity = Literal["restricted", "public"]
Version = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:@/+\-]+$")]


class Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, revalidate_instances="always", hide_input_in_errors=True
    )


class TextPart(Contract):
    type: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=1_000_000, repr=False)


def _validate_data_url(value: str, media_types: tuple[str, ...]) -> str:
    header, separator, payload = value.partition(",")
    if not separator or header not in tuple(f"data:{mime};base64" for mime in media_types):
        raise ValueError("Binary content must be a supported base64 data URL")
    try:
        if not base64.b64decode(payload, validate=True):
            raise ValueError("Binary content must not be empty")
    except (binascii.Error, ValueError):
        raise ValueError("Binary content must contain valid nonempty base64") from None
    return value


class ImagePart(Contract):
    type: Literal["image"] = "image"
    data_url: str = Field(max_length=14_000_000, repr=False)
    detail: Literal["auto", "low", "high"] = "auto"

    @field_validator("data_url")
    @classmethod
    def validate_image(cls, value: str) -> str:
        return _validate_data_url(value, ("image/png", "image/jpeg", "image/webp", "image/gif"))


class FilePart(Contract):
    type: Literal["file"] = "file"
    data_url: str = Field(max_length=14_000_000, repr=False)
    # A fixed transport filename prevents document metadata or paths leaking upstream.
    filename: Literal["invoice.pdf"] = "invoice.pdf"

    @field_validator("data_url")
    @classmethod
    def validate_file(cls, value: str) -> str:
        return _validate_data_url(value, ("application/pdf",))


ContentPart = Annotated[TextPart | ImagePart | FilePart, Field(discriminator="type")]


class GatewayMessage(Contract):
    role: Literal["system", "user", "assistant"]
    content: tuple[ContentPart, ...] = Field(min_length=1, max_length=32, repr=False)

    @model_validator(mode="after")
    def validate_role_content(self) -> Self:
        if self.role != "user" and any(not isinstance(part, TextPart) for part in self.content):
            raise ValueError("Binary content is supported only in user messages")
        return self


class RequestContext(Contract):
    run_id: UUID
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    prompt_version: Version
    scenario: str = Field(default="live", pattern=r"^[a-zA-Z0-9_\-]{1,80}$")
    sensitivity: DataSensitivity = "restricted"


class GatewayRequest(RequestContext):
    alias: ChatAlias
    messages: tuple[GatewayMessage, ...] = Field(min_length=1, max_length=64, repr=False)
    max_output_tokens: int | None = Field(default=None, gt=0)
    semantic_cache: bool = False

    @model_validator(mode="after")
    def public_cache_only(self) -> Self:
        if self.semantic_cache and self.sensitivity != "public":
            raise ValueError("Semantic cache requires explicitly public data")
        if self.semantic_cache and not self.scenario.startswith("public_"):
            raise ValueError("Semantic cache requires a public_ scenario")
        if self.semantic_cache and any(
            not isinstance(part, TextPart) for message in self.messages for part in message.content
        ):
            raise ValueError("Semantic cache accepts text-only requests")
        return self


class EmbeddingRequest(RequestContext):
    alias: Literal["embed"] = "embed"
    inputs: tuple[str, ...] = Field(min_length=1, max_length=128, repr=False)
    dimensions: int | None = Field(default=None, ge=1, le=3072)

    @field_validator("inputs")
    @classmethod
    def validate_inputs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value or len(value) > 1_000_000 for value in values):
            raise ValueError("Embedding inputs must be nonempty bounded strings")
        return values


class TokenUsage(Contract):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_total(self) -> Self:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("Token usage total must match input plus output")
        return self


class GatewayProvenance(Contract):
    alias: ModelAlias
    model: Version
    model_version: Version
    prompt_version: Version


class GatewayResult[T: BaseModel](Contract):
    value: T
    provenance: GatewayProvenance
    usage: TokenUsage
    attempts: int = Field(ge=0)
    latency_ms: float = Field(ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    cache_hit: bool = False
    route_index: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_cache_attempts(self) -> Self:
        if self.cache_hit != (self.attempts == 0):
            raise ValueError("Only a semantic cache hit may have zero gateway attempts")
        return self


class EmbeddingValue(Contract):
    vectors: tuple[tuple[float, ...], ...]

"""Typed model-doorway contracts; document contents are excluded from repr output."""

import base64
import binascii
from decimal import Decimal
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ModelAlias = Literal["extract-vision", "triage-reasoner", "eval-judge", "embed"]
ChatAlias = Literal["extract-vision", "triage-reasoner", "eval-judge"]
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


class GatewayRequest(RequestContext):
    alias: ChatAlias
    messages: tuple[GatewayMessage, ...] = Field(min_length=1, max_length=64, repr=False)
    max_output_tokens: int | None = Field(default=None, gt=0)


class EmbeddingRequest(RequestContext):
    alias: Literal["embed"] = "embed"
    inputs: tuple[str, ...] = Field(min_length=1, max_length=128, repr=False)

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
    attempts: int = Field(ge=1)
    latency_ms: float = Field(ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)


class EmbeddingValue(Contract):
    vectors: tuple[tuple[float, ...], ...]

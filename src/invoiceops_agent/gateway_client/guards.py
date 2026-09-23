"""Deterministic text guards and conservative request accounting before I/O."""

import json
import re
from dataclasses import dataclass

from openai.types.chat import ChatCompletionContentPartParam, ChatCompletionMessageParam
from pydantic import BaseModel

from invoiceops_agent.gateway_client.errors import GuardrailRejected, TokenBudgetExceeded
from invoiceops_agent.gateway_client.schemas import (
    EmbeddingRequest,
    FilePart,
    GatewayRequest,
    ImagePart,
    RequestContext,
    TextPart,
)
from invoiceops_agent.gateway_client.settings import AliasPolicy, GatewaySettings


@dataclass(frozen=True)
class GuardedChat:
    messages: list[ChatCompletionMessageParam]
    output_tokens: int
    schema: dict[str, object]


class RequestGuards:
    def __init__(self, settings: GatewaySettings) -> None:
        self._settings = settings
        self._pii = tuple(
            (name, re.compile(pattern, re.IGNORECASE))
            for name, pattern in settings.pii_patterns.items()
        )
        self._injection = tuple(
            re.compile(pattern, re.IGNORECASE) for pattern in settings.injection_patterns
        )

    def text(self, text: str, context: RequestContext, *, inspect: bool = True) -> str:
        if inspect and any(pattern.search(text) for pattern in self._injection):
            raise GuardrailRejected(context)
        for name, pattern in self._pii:
            text = pattern.sub(f"[REDACTED:{name}]", text)
        return text

    def chat(
        self, request: GatewayRequest, policy: AliasPolicy, response_model: type[BaseModel]
    ) -> GuardedChat:
        messages: list[ChatCompletionMessageParam] = []
        schema = response_model.model_json_schema()
        # One UTF-8 byte per text token deliberately overestimates ordinary tokenizers.
        tokens = len(json.dumps(schema, ensure_ascii=False).encode("utf-8")) + 32
        binary_count = 0
        for message in request.messages:
            parts: list[ChatCompletionContentPartParam] = []
            texts: list[str] = []
            tokens += 16
            for part in message.content:
                if isinstance(part, TextPart):
                    text = self.text(part.text, request, inspect=message.role == "user")
                    tokens += len(text.encode("utf-8"))
                    texts.append(text)
                    parts.append({"type": "text", "text": text})
                else:
                    binary_count += 1
                    if binary_count > self._settings.max_binary_parts:
                        raise TokenBudgetExceeded(request)
                    self._binary(part, request, policy)
                    tokens += policy.binary_token_reserve
                    if isinstance(part, ImagePart):
                        parts.append(
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": part.data_url,
                                    "detail": part.detail,
                                },
                            }
                        )
                    else:
                        parts.append(
                            {
                                "type": "file",
                                "file": {
                                    "file_data": part.data_url,
                                    "filename": part.filename,
                                },
                            }
                        )
            if message.role == "system":
                messages.append({"role": "system", "content": "\n".join(texts)})
            elif message.role == "assistant":
                messages.append({"role": "assistant", "content": "\n".join(texts)})
            else:
                messages.append({"role": "user", "content": parts})
        output = request.max_output_tokens or policy.output_token_limit
        if output > policy.output_token_limit:
            raise TokenBudgetExceeded(request)
        request_bytes = (
            len(
                json.dumps(
                    {
                        "messages": messages,
                        "schema": schema,
                        "max_tokens": output,
                    }
                ).encode("utf-8")
            )
            + 1024
        )
        self._budget(tokens, output, request_bytes, request, policy)
        return GuardedChat(messages=messages, output_tokens=output, schema=schema)

    def embeddings(self, request: EmbeddingRequest, policy: AliasPolicy) -> list[str]:
        inputs = [self.text(text, request) for text in request.inputs]
        size = sum(len(text.encode("utf-8")) for text in inputs)
        self._budget(size + 16 * len(inputs), 0, size, request, policy)
        return inputs

    def _binary(
        self, part: ImagePart | FilePart, context: RequestContext, policy: AliasPolicy
    ) -> None:
        if (isinstance(part, ImagePart) and not policy.allow_images) or (
            isinstance(part, FilePart) and not policy.allow_pdf
        ):
            raise GuardrailRejected(context)
        payload = part.data_url.partition(",")[2]
        decoded_bytes = len(payload) // 4 * 3 - len(payload) + len(payload.rstrip("="))
        if decoded_bytes > self._settings.max_binary_bytes:
            raise TokenBudgetExceeded(context)

    def _budget(
        self, tokens: int, output: int, size: int, context: RequestContext, policy: AliasPolicy
    ) -> None:
        if (
            tokens > policy.input_token_limit
            or tokens + output > policy.total_token_limit
            or size > self._settings.max_request_bytes
        ):
            raise TokenBudgetExceeded(context)

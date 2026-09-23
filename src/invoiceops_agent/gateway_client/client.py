"""The sole asynchronous SDK doorway, with bounded retries and typed local validation."""

import asyncio
import hashlib
import json
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from time import monotonic
from types import TracebackType
from typing import Literal, Self

import httpx2
from openai import APIConnectionError, APIResponseValidationError, APIStatusError, AsyncOpenAI
from openai.types.chat.completion_create_params import ResponseFormat
from pydantic import BaseModel, ValidationError

from invoiceops_agent.gateway_client.cassettes import CassetteMismatch
from invoiceops_agent.gateway_client.errors import (
    GatewayCassetteMismatch,
    GatewayConfigurationError,
    GatewayDeadlineExceeded,
    GatewayError,
    GatewayRequestRejected,
    GatewayUnavailable,
    InvalidGatewayResponse,
    TokenBudgetExceeded,
)
from invoiceops_agent.gateway_client.guards import RequestGuards
from invoiceops_agent.gateway_client.retry import is_retryable, retry_after
from invoiceops_agent.gateway_client.schemas import (
    EmbeddingRequest,
    EmbeddingValue,
    GatewayProvenance,
    GatewayRequest,
    GatewayResult,
    ModelAlias,
    RequestContext,
    TokenUsage,
)
from invoiceops_agent.gateway_client.settings import AliasPolicy, GatewaySettings
from invoiceops_agent.gateway_client.telemetry import (
    GatewayEvent,
    GatewayTelemetry,
    LoggingTelemetry,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _Response[T: BaseModel]:
    value: T
    usage: TokenUsage
    model: str
    cost: Decimal | None


def _cost(headers: httpx2.Headers) -> Decimal | None:
    value = headers.get("x-litellm-response-cost")
    if value is None:
        return None
    cost = Decimal(value)
    if not cost.is_finite() or cost < 0:
        raise ValueError("Invalid cost metadata")
    return cost


def _headers(context: RequestContext) -> dict[str, str]:
    return {
        "X-InvoiceOps-Prompt-Version": context.prompt_version,
        "X-InvoiceOps-Scenario": context.scenario,
        "X-InvoiceOps-Run-ID": str(context.run_id),
        "X-InvoiceOps-Trace-ID": context.trace_id,
    }


class GatewayClient:
    def __init__(
        self,
        settings: GatewaySettings,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = monotonic,
        utcnow: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        telemetry: GatewayTelemetry | None = None,
    ) -> None:
        self._settings = settings
        self._guards = RequestGuards(settings)
        self._clock = clock
        self._utcnow = utcnow
        self._sleep = sleep
        self._telemetry = telemetry or LoggingTelemetry()
        # SDK debug request logs contain document payloads. This application has one SDK doorway.
        logging.getLogger("openai._base_client").setLevel(logging.WARNING)
        self._sdk = AsyncOpenAI(
            api_key=settings.api_key.get_secret_value(),
            base_url=str(settings.base_url),
            max_retries=0,
            timeout=settings.request_timeout_seconds,
            _strict_response_validation=True,
            http_client=httpx2.AsyncClient(
                transport=transport,
                follow_redirects=False,
                trust_env=False,
                timeout=settings.request_timeout_seconds,
            ),
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._sdk.close()

    async def complete[T: BaseModel](
        self, request: GatewayRequest, response_model: type[T]
    ) -> GatewayResult[T]:
        def prepare(policy: AliasPolicy) -> Callable[[float], Awaitable[_Response[T]]]:
            guarded = self._guards.chat(request, policy, response_model)
            headers = _headers(request)
            headers["X-InvoiceOps-Schema-Hash"] = hashlib.sha256(
                json.dumps(guarded.schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            response_format: ResponseFormat
            if policy.response_format == "json_schema":
                response_format = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "gateway_response",
                        "strict": True,
                        "schema": guarded.schema,
                    },
                }
            elif policy.response_format == "json_object":
                response_format = {"type": "json_object"}
            else:
                response_format = {"type": "text"}

            async def operation(request_timeout: float) -> _Response[T]:
                raw = await self._sdk.chat.completions.with_raw_response.create(
                    model=request.alias,
                    messages=guarded.messages,
                    max_tokens=guarded.output_tokens,
                    response_format=response_format,
                    extra_headers=headers,
                    timeout=request_timeout,
                )
                completion = raw.parse()
                if len(completion.choices) != 1 or completion.usage is None:
                    raise InvalidGatewayResponse(request)
                choice = completion.choices[0]
                if (
                    choice.finish_reason != "stop"
                    or choice.message.refusal
                    or choice.message.tool_calls
                    or choice.message.function_call
                    or not choice.message.content
                ):
                    raise InvalidGatewayResponse(request)
                value = response_model.model_validate_json(choice.message.content, strict=True)
                usage = TokenUsage(
                    input_tokens=completion.usage.prompt_tokens,
                    output_tokens=completion.usage.completion_tokens,
                    total_tokens=completion.usage.total_tokens,
                )
                if usage.output_tokens > guarded.output_tokens:
                    raise TokenBudgetExceeded(request)
                return _Response(value, usage, completion.model, _cost(raw.headers))

            return operation

        return await self._invoke(request, request.alias, prepare)

    async def embed(self, request: EmbeddingRequest) -> GatewayResult[EmbeddingValue]:
        def prepare(
            policy: AliasPolicy,
        ) -> Callable[[float], Awaitable[_Response[EmbeddingValue]]]:
            inputs = self._guards.embeddings(request, policy)

            async def operation(request_timeout: float) -> _Response[EmbeddingValue]:
                raw = await self._sdk.embeddings.with_raw_response.create(
                    input=inputs,
                    model="embed",
                    encoding_format="float",
                    extra_headers=_headers(request),
                    timeout=request_timeout,
                )
                completion = raw.parse()
                ordered = sorted(completion.data, key=lambda item: item.index)
                vectors = tuple(tuple(item.embedding) for item in ordered)
                if (
                    [item.index for item in ordered] != list(range(len(inputs)))
                    or not vectors
                    or not vectors[0]
                    or any(len(vector) != len(vectors[0]) for vector in vectors)
                    or any(not math.isfinite(value) for vector in vectors for value in vector)
                ):
                    raise InvalidGatewayResponse(request)
                usage = TokenUsage(
                    input_tokens=completion.usage.prompt_tokens,
                    output_tokens=0,
                    total_tokens=completion.usage.total_tokens,
                )
                return _Response(
                    EmbeddingValue(vectors=vectors), usage, completion.model, _cost(raw.headers)
                )

            return operation

        return await self._invoke(request, request.alias, prepare)

    async def _invoke[T: BaseModel](
        self,
        context: RequestContext,
        alias: ModelAlias,
        prepare: Callable[[AliasPolicy], Callable[[float], Awaitable[_Response[T]]]],
    ) -> GatewayResult[T]:
        policy = self._settings.aliases.get(alias)
        if policy is None:
            raise GatewayConfigurationError(context)
        started = self._clock()
        deadline = started + self._settings.total_timeout_seconds
        attempts = 0
        response: _Response[T] | None = None
        status: Literal["succeeded", "failed", "cancelled"] = "failed"
        error_code: str | None = None
        try:
            async with asyncio.timeout(self._settings.total_timeout_seconds):
                operation = prepare(policy)
                while True:
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        raise GatewayDeadlineExceeded(context, attempts=attempts)
                    attempts += 1
                    timeout = min(remaining, self._settings.request_timeout_seconds)
                    try:
                        async with asyncio.timeout(timeout):
                            response = await operation(timeout)
                        if (
                            response.usage.input_tokens > policy.input_token_limit
                            or response.usage.total_tokens > policy.total_token_limit
                        ):
                            raise TokenBudgetExceeded(context, attempts=attempts)
                        break
                    except CassetteMismatch:
                        raise GatewayCassetteMismatch(context, attempts=attempts) from None
                    except (APIConnectionError, APIStatusError) as error:
                        if isinstance(error.__cause__, CassetteMismatch):
                            raise GatewayCassetteMismatch(context, attempts=attempts) from None
                        if not is_retryable(error):
                            raise GatewayRequestRejected(context, attempts=attempts) from None
                        hint = (
                            retry_after(error, self._utcnow())
                            if isinstance(error, APIStatusError)
                            else None
                        )
                    except TimeoutError:
                        hint = None
                    if attempts >= self._settings.max_attempts:
                        raise GatewayUnavailable(context, attempts=attempts) from None
                    delay = (
                        hint
                        if hint is not None
                        else min(
                            self._settings.backoff_seconds * 2 ** (attempts - 1),
                            self._settings.max_retry_delay_seconds,
                        )
                    )
                    if delay > self._settings.max_retry_delay_seconds:
                        raise GatewayUnavailable(context, attempts=attempts) from None
                    if delay >= deadline - self._clock():
                        raise GatewayDeadlineExceeded(context, attempts=attempts) from None
                    await self._sleep(delay)
                result = GatewayResult[T](
                    value=response.value,
                    provenance=GatewayProvenance(
                        alias=alias,
                        model=response.model,
                        model_version=policy.model_version,
                        prompt_version=context.prompt_version,
                    ),
                    usage=response.usage,
                    attempts=attempts,
                    latency_ms=max(0, self._clock() - started) * 1000,
                    cost_usd=response.cost,
                )
                status = "succeeded"
                return result
        except TimeoutError:
            error_code = GatewayDeadlineExceeded.code
            raise GatewayDeadlineExceeded(context, attempts=attempts) from None
        except (
            ValidationError,
            APIResponseValidationError,
            json.JSONDecodeError,
            ValueError,
            TypeError,
            InvalidOperation,
        ):
            error_code = InvalidGatewayResponse.code
            raise InvalidGatewayResponse(context, attempts=attempts) from None
        except GatewayError as error:
            error.attempts = attempts
            error_code = error.code
            raise
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            self._telemetry.record(
                GatewayEvent(
                    run_id=context.run_id,
                    trace_id=context.trace_id,
                    prompt_version=context.prompt_version,
                    scenario=context.scenario,
                    alias=alias,
                    model=response.model if response and status == "succeeded" else None,
                    model_version=policy.model_version,
                    status=status,
                    attempts=attempts,
                    latency_ms=max(0, self._clock() - started) * 1000,
                    usage=response.usage if response else None,
                    cost_usd=response.cost if response else None,
                    error_code=error_code,
                )
            )

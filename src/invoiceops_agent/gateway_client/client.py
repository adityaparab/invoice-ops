"""The sole asynchronous SDK doorway, with bounded retries and typed local validation."""

import asyncio
import hashlib
import json
import logging
import math
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from time import monotonic
from types import TracebackType
from typing import Literal, Self

import httpx2
from openai import APIConnectionError, APIResponseValidationError, APIStatusError, AsyncOpenAI
from openai.types.chat.completion_create_params import ResponseFormat
from opentelemetry import trace
from pydantic import BaseModel, ValidationError

from invoiceops_agent.gateway_client.budgets import BudgetAlertTracker
from invoiceops_agent.gateway_client.cache import (
    CacheEntry,
    SemanticCache,
    SemanticCacheUnavailable,
    cache_identity,
)
from invoiceops_agent.gateway_client.cassettes import (
    AliasCassetteTransport,
    CassetteMismatch,
    CassetteTransport,
)
from invoiceops_agent.gateway_client.errors import (
    GatewayCassetteMismatch,
    GatewayConfigurationError,
    GatewayDeadlineExceeded,
    GatewayError,
    GatewayRequestRejected,
    GatewayUnavailable,
    InvalidGatewayResponse,
    InvalidStructuredOutput,
    TokenBudgetExceeded,
)
from invoiceops_agent.gateway_client.guards import GuardedChat, RequestGuards
from invoiceops_agent.gateway_client.retry import is_retryable, retry_after
from invoiceops_agent.gateway_client.schemas import (
    EmbeddingRequest,
    EmbeddingValue,
    GatewayProvenance,
    GatewayRequest,
    GatewayResult,
    ModelAlias,
    RequestContext,
    TextPart,
    TokenUsage,
)
from invoiceops_agent.gateway_client.settings import AliasPolicy, GatewaySettings
from invoiceops_agent.gateway_client.telemetry import (
    GatewayEvent,
    GatewayTelemetry,
    LoggingTelemetry,
    SpanGatewayTelemetry,
    gateway_span,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _Response[T: BaseModel]:
    value: T
    usage: TokenUsage
    model: str
    cost: Decimal | None


@dataclass(frozen=True)
class _CacheProbe:
    namespace: str
    vector: tuple[float, ...]
    hit: CacheEntry | None
    primary_name: str
    started: float


def _cost(headers: httpx2.Headers) -> Decimal | None:
    value = headers.get("x-litellm-response-cost")
    if value is None:
        return None
    cost = Decimal(value)
    if not cost.is_finite() or cost < 0:
        raise ValueError("Invalid cost metadata")
    return cost


def _headers(context: RequestContext, alias: ModelAlias | None = None) -> dict[str, str]:
    headers = {
        "X-InvoiceOps-Prompt-Version": context.prompt_version,
        "X-InvoiceOps-Scenario": context.scenario,
        "X-InvoiceOps-Run-ID": str(context.run_id),
        "X-InvoiceOps-Trace-ID": context.trace_id,
    }
    if alias is not None:
        headers["X-InvoiceOps-Alias"] = alias
    return headers


class _RequestBodyTooLarge(Exception):
    """The SDK-serialized body exceeded its pre-transport byte limit."""


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
        semantic_cache: SemanticCache | None = None,
    ) -> None:
        self._settings = settings
        self._cassettes = transport if isinstance(transport, CassetteTransport) else None
        self._guards = RequestGuards(settings)
        self._clock = clock
        self._utcnow = utcnow
        self._sleep = sleep
        self._telemetry = SpanGatewayTelemetry(telemetry or LoggingTelemetry())
        self._semantic_cache = semantic_cache
        self._budgets = BudgetAlertTracker(settings.budget_alert_usd)
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
                event_hooks={"request": [self._check_request_body]},
                follow_redirects=False,
                trust_env=False,
                timeout=settings.request_timeout_seconds,
            ),
        )

    def configured_policy(self, alias: ModelAlias, context: RequestContext) -> AliasPolicy:
        """Expose the immutable configured policy so audit pins also cover failed calls."""
        policy = self._settings.aliases.get(alias)
        if policy is None:
            raise GatewayConfigurationError(context)
        return policy

    async def _check_request_body(self, request: httpx2.Request) -> None:
        if len(request.content) > self._settings.max_request_bytes:
            raise _RequestBodyTooLarge

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
        def prepare(
            policy: AliasPolicy, model_name: str
        ) -> Callable[[float], Awaitable[_Response[T]]]:
            guarded = self._guards.chat(request, policy, response_model)
            headers = _headers(
                request,
                request.alias if isinstance(self._cassettes, AliasCassetteTransport) else None,
            )
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
                    model=model_name,
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
                try:
                    value = response_model.model_validate_json(choice.message.content, strict=True)
                except ValidationError:
                    raise InvalidStructuredOutput(request) from None
                usage = TokenUsage(
                    input_tokens=completion.usage.prompt_tokens,
                    output_tokens=completion.usage.completion_tokens,
                    total_tokens=completion.usage.total_tokens,
                )
                if usage.output_tokens > guarded.output_tokens:
                    raise TokenBudgetExceeded(request)
                return _Response(value, usage, completion.model, _cost(raw.headers))

            return operation

        if request.semantic_cache and self._semantic_cache is not None:
            return await self._complete_cached(request, response_model, prepare)
        return await self._invoke(request, request.alias, prepare)

    async def _complete_cached[T: BaseModel](
        self,
        request: GatewayRequest,
        response_model: type[T],
        prepare: Callable[[AliasPolicy, str], Callable[[float], Awaitable[_Response[T]]]],
    ) -> GatewayResult[T]:
        if request.sensitivity != "public" or not request.scenario.startswith("public_"):
            raise GatewayRequestRejected(request)
        cache = self._semantic_cache
        if cache is None:
            return await self._invoke(request, request.alias, prepare)
        if any(
            isinstance(part, TextPart) and self._guards.contains_pii(part.text)
            for message in request.messages
            for part in message.content
        ):
            return await self._invoke(request, request.alias, prepare)
        policy = self.configured_policy(request.alias, request)
        primary_name = policy.routes_for("public", request.alias)[0][0]
        embed_policy = self._settings.aliases.get("embed")
        if embed_policy is None:
            return await self._invoke(request, request.alias, prepare)
        embed_name = embed_policy.routes_for("public", "embed")[0][0]
        guarded = self._guards.chat(request, policy, response_model)
        probe = await self._probe_cache(request, guarded, primary_name, embed_name, cache)
        if probe is not None and probe.hit is not None:
            try:
                return self._cache_hit_result(request, response_model, probe)
            except ValidationError:
                logger.warning(
                    "event=gateway.semantic_cache_invalid_entry run_id=%s alias=%s",
                    request.run_id,
                    request.alias,
                )
        result = await self._invoke(request, request.alias, prepare)
        if probe is not None and result.route_index == 0:
            await self._store_cache(request, cache, probe, result)
        return result

    async def _probe_cache(
        self,
        request: GatewayRequest,
        guarded: GuardedChat,
        primary_name: str,
        embed_name: str,
        cache: SemanticCache,
    ) -> _CacheProbe | None:
        namespace, prompt = cache_identity(
            request,
            guarded_messages=guarded.messages,
            response_schema=guarded.schema,
            model_name=primary_name,
            embedding_model_name=embed_name,
        )
        if len(prompt.encode("utf-8")) > 1_000_000:
            return None
        started = self._clock()
        try:
            async with asyncio.timeout(self._settings.semantic_cache_probe_timeout_seconds):
                embedded = await self.embed(
                    EmbeddingRequest(
                        run_id=request.run_id,
                        trace_id=request.trace_id,
                        prompt_version="semantic-cache-v1",
                        scenario="public_cache",
                        sensitivity="public",
                        inputs=(prompt,),
                    )
                )
                vector = embedded.value.vectors[0]
                namespace = hashlib.sha256(f"{namespace}:{len(vector)}".encode("ascii")).hexdigest()
                hit = await cache.find(
                    namespace,
                    vector,
                    minimum_similarity=self._settings.semantic_cache_min_similarity,
                    now=self._utcnow(),
                )
            return _CacheProbe(namespace, vector, hit, primary_name, started)
        except (
            GatewayError,
            SemanticCacheUnavailable,
            TimeoutError,
            ValidationError,
            ValueError,
        ) as error:
            logger.warning(
                "event=gateway.semantic_cache_bypass run_id=%s alias=%s error_type=%s",
                request.run_id,
                request.alias,
                type(error).__name__,
            )
            return None

    def _cache_hit_result[T: BaseModel](
        self, request: GatewayRequest, response_model: type[T], probe: _CacheProbe
    ) -> GatewayResult[T]:
        hit = probe.hit
        if hit is None:
            raise ValueError("No cache hit is available")
        value = response_model.model_validate_json(hit.value_json, strict=True)
        result = GatewayResult[T](
            value=value,
            provenance=GatewayProvenance(
                alias=request.alias,
                model=hit.model,
                model_version=hit.model_version,
                prompt_version=request.prompt_version,
            ),
            usage=TokenUsage(input_tokens=0, output_tokens=0, total_tokens=0),
            attempts=0,
            latency_ms=max(0, self._clock() - probe.started) * 1000,
            cache_hit=True,
        )
        with trace.get_tracer("invoiceops.gateway").start_as_current_span(
            "invoiceops.gateway.cache_hit"
        ):
            self._telemetry.record(
                GatewayEvent(
                    run_id=request.run_id,
                    trace_id=request.trace_id,
                    prompt_version=request.prompt_version,
                    scenario=request.scenario,
                    alias=request.alias,
                    requested_model=probe.primary_name,
                    model=hit.model,
                    model_version=hit.model_version,
                    status="succeeded",
                    attempts=0,
                    latency_ms=result.latency_ms,
                    cache_hit=True,
                )
            )
        return result

    async def _store_cache[T: BaseModel](
        self,
        request: GatewayRequest,
        cache: SemanticCache,
        probe: _CacheProbe,
        result: GatewayResult[T],
    ) -> None:
        response_json = result.value.model_dump_json()
        if self._guards.contains_pii(response_json):
            return
        try:
            cache_now = self._utcnow()
            async with asyncio.timeout(self._settings.semantic_cache_store_timeout_seconds):
                await cache.store(
                    probe.namespace,
                    probe.vector,
                    CacheEntry(
                        value_json=response_json,
                        model=result.provenance.model,
                        model_version=result.provenance.model_version,
                    ),
                    now=cache_now,
                    expires_at=cache_now
                    + timedelta(seconds=self._settings.semantic_cache_ttl_seconds),
                )
        except (SemanticCacheUnavailable, TimeoutError, ValueError) as error:
            logger.warning(
                "event=gateway.semantic_cache_store_failed run_id=%s alias=%s error_type=%s",
                request.run_id,
                request.alias,
                type(error).__name__,
            )

    async def embed(self, request: EmbeddingRequest) -> GatewayResult[EmbeddingValue]:
        def prepare(
            policy: AliasPolicy,
            model_name: str,
        ) -> Callable[[float], Awaitable[_Response[EmbeddingValue]]]:
            inputs = self._guards.embeddings(request, policy)

            async def operation(request_timeout: float) -> _Response[EmbeddingValue]:
                raw = await self._sdk.embeddings.with_raw_response.create(
                    input=inputs,
                    model=model_name,
                    encoding_format="float",
                    extra_headers=_headers(
                        request,
                        request.alias
                        if isinstance(self._cassettes, AliasCassetteTransport)
                        else None,
                    ),
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
        prepare: Callable[[AliasPolicy, str], Callable[[float], Awaitable[_Response[T]]]],
    ) -> GatewayResult[T]:
        with gateway_span(context, alias):
            return await self._invoke_core(context, alias, prepare)

    async def _invoke_core[T: BaseModel](
        self,
        context: RequestContext,
        alias: ModelAlias,
        prepare: Callable[[AliasPolicy, str], Callable[[float], Awaitable[_Response[T]]]],
    ) -> GatewayResult[T]:
        policy = self.configured_policy(alias, context)
        routes = policy.routes_for(context.sensitivity, alias)
        selected_name, selected_version = routes[0]
        route_index = 0
        started = self._clock()
        deadline = started + self._settings.total_timeout_seconds
        attempts = 0
        response: _Response[T] | None = None
        status: Literal["succeeded", "failed", "cancelled"] = "failed"
        error_code: str | None = None
        observed_run_cost_usd: Decimal | None = None
        budget_alert = False
        try:
            async with (
                asyncio.timeout(self._settings.total_timeout_seconds),
                self._cassettes.invocation() if self._cassettes else nullcontext(),
            ):
                for index, route in enumerate(routes):
                    route_index = index
                    selected_name, selected_version = route
                    operation = prepare(policy, selected_name)
                    route_attempts = 0
                    while True:
                        remaining = deadline - self._clock()
                        if remaining <= 0:
                            raise GatewayDeadlineExceeded(context, attempts=attempts)
                        attempts += 1
                        route_attempts += 1
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
                        if route_attempts >= self._settings.max_attempts:
                            break
                        delay = (
                            hint
                            if hint is not None
                            else min(
                                self._settings.backoff_seconds * 2 ** (route_attempts - 1),
                                self._settings.max_retry_delay_seconds,
                            )
                        )
                        if delay > self._settings.max_retry_delay_seconds:
                            break
                        if delay >= deadline - self._clock():
                            raise GatewayDeadlineExceeded(context, attempts=attempts) from None
                        await self._sleep(delay)
                    if response is not None:
                        break
                if response is None:
                    raise GatewayUnavailable(context, attempts=attempts)
                result = GatewayResult[T](
                    value=response.value,
                    provenance=GatewayProvenance(
                        alias=alias,
                        model=response.model,
                        model_version=selected_version,
                        prompt_version=context.prompt_version,
                    ),
                    usage=response.usage,
                    attempts=attempts,
                    latency_ms=max(0, self._clock() - started) * 1000,
                    cost_usd=response.cost,
                    route_index=route_index,
                )
            status = "succeeded"
            observed_run_cost_usd, budget_alert = self._budgets.observe(
                context.run_id, response.cost
            )
            return result
        except _RequestBodyTooLarge:
            error_code = TokenBudgetExceeded.code
            raise TokenBudgetExceeded(context, attempts=attempts) from None
        except CassetteMismatch:
            status = "failed"
            error_code = GatewayCassetteMismatch.code
            raise GatewayCassetteMismatch(context, attempts=attempts) from None
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
                    requested_model=selected_name,
                    model=response.model if response and status == "succeeded" else None,
                    model_version=selected_version,
                    route_index=route_index,
                    observed_run_cost_usd=observed_run_cost_usd,
                    budget_alert=budget_alert,
                    status=status,
                    attempts=attempts,
                    latency_ms=max(0, self._clock() - started) * 1000,
                    usage=response.usage if response else None,
                    cost_usd=response.cost if response else None,
                    error_code=error_code,
                )
            )

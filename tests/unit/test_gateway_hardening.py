"""Public semantic cache and observed-cost alert behavior without network access."""

import math
from datetime import datetime
from decimal import Decimal
from uuid import UUID

import httpx2
import pytest
from pydantic import BaseModel, ValidationError

from invoiceops_agent.gateway_client.budgets import BudgetAlertTracker
from invoiceops_agent.gateway_client.cache import CacheEntry, SemanticCacheUnavailable
from invoiceops_agent.gateway_client.client import GatewayClient
from invoiceops_agent.gateway_client.schemas import (
    GatewayMessage,
    GatewayRequest,
    ImagePart,
    TextPart,
)
from invoiceops_agent.gateway_client.settings import AliasPolicy, GatewaySettings
from invoiceops_agent.gateway_client.telemetry import GatewayEvent

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class PublicAnswer(BaseModel):
    answer: str


class MemoryCache:
    def __init__(self) -> None:
        self.entries: dict[str, tuple[tuple[float, ...], CacheEntry]] = {}

    async def find(
        self,
        namespace: str,
        vector: tuple[float, ...],
        *,
        minimum_similarity: float,
        now: datetime,
    ) -> CacheEntry | None:
        item = self.entries.get(namespace)
        if item is None:
            return None
        stored_vector, entry = item
        similarity = sum(left * right for left, right in zip(vector, stored_vector, strict=True))
        similarity /= math.sqrt(sum(value * value for value in vector))
        similarity /= math.sqrt(sum(value * value for value in stored_vector))
        return entry if similarity >= minimum_similarity else None

    async def store(
        self,
        namespace: str,
        vector: tuple[float, ...],
        entry: CacheEntry,
        *,
        now: datetime,
        expires_at: datetime,
    ) -> None:
        self.entries[namespace] = (vector, entry)


class Events:
    def __init__(self) -> None:
        self.events: list[GatewayEvent] = []

    def record(self, event: GatewayEvent) -> None:
        self.events.append(event)


def public_request(text: str, *, prompt_version: str = "faq-v1") -> GatewayRequest:
    return GatewayRequest(
        run_id=UUID(int=17),
        trace_id="f" * 32,
        prompt_version=prompt_version,
        scenario="public_faq",
        sensitivity="public",
        semantic_cache=True,
        alias="triage-reasoner",
        messages=(GatewayMessage(role="user", content=(TextPart(text=text),)),),
    )


def settings() -> GatewaySettings:
    return GatewaySettings(
        base_url="http://gateway.test/v1",
        api_key="synthetic-key",
        aliases={
            "triage-reasoner": AliasPolicy(
                model_version="public-chat-v1", model_name="public-chat-v1"
            ),
            "embed": AliasPolicy(model_version="public-embed-v1", model_name="public-embed-v1"),
        },
    )


def respond(call: httpx2.Request) -> httpx2.Response:
    if call.url.path.endswith("/embeddings"):
        return httpx2.Response(
            200,
            json={
                "object": "list",
                "model": "public-embed-v1",
                "data": [{"object": "embedding", "index": 0, "embedding": [1.0, 0.0]}],
                "usage": {"prompt_tokens": 10, "total_tokens": 10},
            },
        )
    return httpx2.Response(
        200,
        headers={"x-litellm-response-cost": "0.002"},
        json={
            "id": "chatcmpl-public-fixture",
            "object": "chat.completion",
            "created": 0,
            "model": "public-chat-v1",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": '{"answer":"Paris"}'},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        },
    )


async def test_public_semantic_cache_hit_skips_chat_and_does_not_duplicate_spend() -> None:
    seen: list[str] = []
    cache = MemoryCache()
    telemetry = Events()

    def handle(call: httpx2.Request) -> httpx2.Response:
        seen.append(call.url.path)
        return respond(call)

    async with GatewayClient(
        settings(),
        transport=httpx2.MockTransport(handle),
        semantic_cache=cache,
        telemetry=telemetry,
    ) as gateway:
        first = await gateway.complete(public_request("What is France's capital?"), PublicAnswer)
        second = await gateway.complete(public_request("Name the capital of France."), PublicAnswer)
    assert seen == ["/v1/embeddings", "/v1/chat/completions", "/v1/embeddings"]
    assert first.value.answer == second.value.answer == "Paris"
    assert first.cache_hit is False and first.cost_usd == Decimal("0.002")
    assert second.cache_hit is True and second.attempts == 0 and second.cost_usd is None
    assert second.usage.total_tokens == 0
    assert telemetry.events[-1].cache_hit is True
    assert len(cache.entries) == 1


async def test_cache_namespace_pins_numbers_and_prompt_version() -> None:
    cache = MemoryCache()
    chat_calls = 0

    def handle(call: httpx2.Request) -> httpx2.Response:
        nonlocal chat_calls
        if call.url.path.endswith("/chat/completions"):
            chat_calls += 1
        return respond(call)

    async with GatewayClient(
        settings(), transport=httpx2.MockTransport(handle), semantic_cache=cache
    ) as gateway:
        await gateway.complete(public_request("Rule for 2025?"), PublicAnswer)
        await gateway.complete(public_request("Rule for 2026?"), PublicAnswer)
        await gateway.complete(
            public_request("Rule for 2025?", prompt_version="faq-v2"), PublicAnswer
        )
    assert chat_calls == 3
    assert len(cache.entries) == 3


async def test_restricted_or_binary_content_cannot_request_semantic_cache() -> None:
    with pytest.raises(ValidationError):
        GatewayRequest.model_validate(
            {**public_request("Public question").model_dump(), "sensitivity": "restricted"}
        )
    with pytest.raises(ValidationError):
        GatewayRequest.model_validate(
            {**public_request("Public question").model_dump(), "scenario": "invoice"}
        )
    with pytest.raises(ValidationError):
        GatewayRequest.model_validate(
            {
                **public_request("Public question").model_dump(),
                "messages": (
                    GatewayMessage(
                        role="user",
                        content=(ImagePart(data_url="data:image/png;base64,aQ=="),),
                    ),
                ),
            }
        )


async def test_semantic_cache_store_failure_preserves_successful_model_response() -> None:
    class FailingStore(MemoryCache):
        async def store(
            self,
            namespace: str,
            vector: tuple[float, ...],
            entry: CacheEntry,
            *,
            now: datetime,
            expires_at: datetime,
        ) -> None:
            raise SemanticCacheUnavailable("synthetic cache outage")

    async with GatewayClient(
        settings(), transport=httpx2.MockTransport(respond), semantic_cache=FailingStore()
    ) as gateway:
        result = await gateway.complete(public_request("What is France's capital?"), PublicAnswer)
    assert result.value.answer == "Paris"
    assert result.cache_hit is False


async def test_public_cache_skips_inputs_with_recognizable_pii() -> None:
    cache = MemoryCache()
    seen: list[str] = []

    def handle(call: httpx2.Request) -> httpx2.Response:
        seen.append(call.url.path)
        return respond(call)

    async with GatewayClient(
        settings(), transport=httpx2.MockTransport(handle), semantic_cache=cache
    ) as gateway:
        result = await gateway.complete(
            public_request("Reply to synthetic@example.test"), PublicAnswer
        )
    assert result.cache_hit is False
    assert seen == ["/v1/chat/completions"]
    assert cache.entries == {}


async def test_observed_cost_alert_crosses_threshold_once() -> None:
    tracker = BudgetAlertTracker(Decimal("0.04"))
    run_id = UUID(int=5)
    assert tracker.observe(run_id, Decimal("0.02")) == (Decimal("0.02"), False)
    assert tracker.observe(run_id, None) == (Decimal("0.02"), False)
    assert tracker.observe(run_id, Decimal("0.02")) == (Decimal("0.04"), True)
    assert tracker.observe(run_id, Decimal("0.01")) == (Decimal("0.05"), False)

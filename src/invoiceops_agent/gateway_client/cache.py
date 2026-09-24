"""Explicit public-data semantic cache contract and stable namespace construction."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from invoiceops_agent.gateway_client.schemas import GatewayRequest

_ANCHOR = re.compile(r"\b(?:[A-Z]{2,}[-_]?)?\d+(?:[.,]\d+)?\b")


@dataclass(frozen=True)
class CacheEntry:
    value_json: str
    model: str
    model_version: str


class SemanticCacheUnavailable(Exception):
    """A cache storage operation failed; the model call may proceed uncached."""


class SemanticCache(Protocol):
    async def find(
        self,
        namespace: str,
        vector: tuple[float, ...],
        *,
        minimum_similarity: float,
        now: datetime,
    ) -> CacheEntry | None: ...

    async def store(
        self,
        namespace: str,
        vector: tuple[float, ...],
        entry: CacheEntry,
        *,
        now: datetime,
        expires_at: datetime,
    ) -> None: ...


def cache_identity(
    request: GatewayRequest,
    *,
    guarded_messages: object,
    response_schema: object,
    model_name: str,
    embedding_model_name: str,
) -> tuple[str, str]:
    """Return a namespace hash and text to embed; never persist request text."""
    prompt = json.dumps(guarded_messages, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    anchors = sorted(_ANCHOR.findall(prompt))
    namespace = hashlib.sha256(
        json.dumps(
            {
                "alias": request.alias,
                "scenario": request.scenario,
                "prompt_version": request.prompt_version,
                "model": model_name,
                "embedding_model": embedding_model_name,
                "schema": response_schema,
                "anchors": anchors,
                "policy": "semantic-cache-v1",
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return namespace, prompt

"""Restricted-role pgvector cache lookup, isolation, and expiration."""

import json
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from tests.integration.test_ledger import ledger_runtime_dsn as ledger_runtime_dsn
from tests.integration.test_ledger import runtime_connection

from invoiceops_agent.gateway_client.cache import CacheEntry
from invoiceops_agent.tools.semantic_cache import PostgresSemanticCache

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_public_semantic_cache_uses_pgvector_and_expires(
    migrated_database: psycopg.Connection[tuple[object, ...]], ledger_runtime_dsn: str
) -> None:
    cache = PostgresSemanticCache(lambda: runtime_connection(ledger_runtime_dsn))
    now = datetime(2026, 9, 24, 12, tzinfo=UTC)
    namespace = "a" * 64
    entry = CacheEntry('{"answer":"Paris"}', "public-chat-v1", "public-chat-v1")
    await cache.store(namespace, (1.0, 0.0), entry, now=now, expires_at=now + timedelta(hours=1))
    hit = await cache.find(namespace, (0.999, 0.001), minimum_similarity=0.995, now=now)
    assert hit is not None
    assert json.loads(hit.value_json) == {"answer": "Paris"}
    assert hit.model == hit.model_version == "public-chat-v1"
    assert await cache.find(namespace, (0.0, 1.0), minimum_similarity=0.995, now=now) is None
    assert await cache.find("b" * 64, (1.0, 0.0), minimum_similarity=0.995, now=now) is None
    assert (
        await cache.find(
            namespace, (1.0, 0.0), minimum_similarity=0.995, now=now + timedelta(hours=2)
        )
        is None
    )
    row = migrated_database.execute(
        "SELECT response_json, model, model_version FROM public.gateway_semantic_cache "
        "WHERE namespace = %s",
        (namespace,),
    ).fetchone()
    assert row is not None
    assert row[0] == {"answer": "Paris"}
    assert row[1:] == ("public-chat-v1", "public-chat-v1")

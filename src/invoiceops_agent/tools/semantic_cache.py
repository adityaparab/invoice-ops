"""Persistent pgvector cache for explicitly public, text-only gateway requests."""

import hashlib
import logging
import math
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime

import psycopg
from psycopg.rows import DictRow

from invoiceops_agent.gateway_client.cache import CacheEntry, SemanticCacheUnavailable

logger = logging.getLogger(__name__)
ConnectionFactory = Callable[[], AbstractAsyncContextManager[psycopg.AsyncConnection[DictRow]]]


def _vector_literal(vector: tuple[float, ...]) -> str:
    if not 1 <= len(vector) <= 4096 or any(not math.isfinite(value) for value in vector):
        raise ValueError("Semantic cache embedding must contain finite dimensions")
    if not any(value != 0 for value in vector):
        raise ValueError("Semantic cache embedding cannot be zero")
    return "[" + ",".join(repr(value) for value in vector) + "]"


class PostgresSemanticCache:
    def __init__(self, connection: ConnectionFactory) -> None:
        self._connection = connection

    async def find(
        self,
        namespace: str,
        vector: tuple[float, ...],
        *,
        minimum_similarity: float,
        now: datetime,
    ) -> CacheEntry | None:
        literal = _vector_literal(vector)
        try:
            async with self._connection() as connection:
                cursor = await connection.execute(
                    "WITH candidates AS MATERIALIZED ("
                    "SELECT cache_key, embedding, response_json, model, model_version "
                    "FROM public.gateway_semantic_cache "
                    "WHERE namespace = %s AND expires_at > %s) "
                    "SELECT cache_key, response_json::text AS value_json, model, model_version, "
                    "embedding <=> %s::vector AS distance FROM candidates "
                    "ORDER BY distance, cache_key LIMIT 1",
                    (namespace, now, literal),
                )
                row = await cursor.fetchone()
        except psycopg.Error as error:
            logger.warning("event=semantic_cache.find_failed error_type=%s", type(error).__name__)
            raise SemanticCacheUnavailable("Semantic cache lookup failed") from None
        if row is None or row["distance"] is None:
            return None
        if 1 - float(row["distance"]) < minimum_similarity:
            return None
        return CacheEntry(
            value_json=row["value_json"],
            model=row["model"],
            model_version=row["model_version"],
        )

    async def store(
        self,
        namespace: str,
        vector: tuple[float, ...],
        entry: CacheEntry,
        *,
        now: datetime,
        expires_at: datetime,
    ) -> None:
        literal = _vector_literal(vector)
        cache_key = hashlib.sha256((namespace + literal).encode("ascii")).hexdigest()
        try:
            async with self._connection() as connection, connection.transaction():
                await connection.execute(
                    "INSERT INTO public.gateway_semantic_cache "
                    "(cache_key, namespace, embedding, response_json, model, model_version, "
                    "created_at, expires_at) "
                    "VALUES (%s, %s, %s::vector, %s::jsonb, %s, %s, %s, %s) "
                    "ON CONFLICT (cache_key) DO UPDATE SET response_json = EXCLUDED.response_json, "
                    "model = EXCLUDED.model, model_version = EXCLUDED.model_version, "
                    "created_at = EXCLUDED.created_at, expires_at = EXCLUDED.expires_at",
                    (
                        cache_key,
                        namespace,
                        literal,
                        entry.value_json,
                        entry.model,
                        entry.model_version,
                        now,
                        expires_at,
                    ),
                )
                await connection.execute(
                    "DELETE FROM public.gateway_semantic_cache WHERE expires_at <= %s", (now,)
                )
                await connection.execute(
                    "DELETE FROM public.gateway_semantic_cache WHERE cache_key IN ("
                    "SELECT cache_key FROM public.gateway_semantic_cache "
                    "WHERE namespace = %s ORDER BY created_at DESC, cache_key DESC OFFSET 10000)",
                    (namespace,),
                )
        except psycopg.Error as error:
            logger.warning("event=semantic_cache.store_failed error_type=%s", type(error).__name__)
            raise SemanticCacheUnavailable("Semantic cache store failed") from None

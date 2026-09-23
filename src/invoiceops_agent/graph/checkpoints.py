"""Owned Postgres checkpoint connections, restricted serialization, and run locks."""

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.rows import DictRow, dict_row

from invoiceops_agent.graph.errors import CheckpointUnavailable, RunInProgress
from invoiceops_agent.graph.hello import build_hello_graph
from invoiceops_agent.graph.nodes.hello import HelloNodes
from invoiceops_agent.graph.runner import GraphRunner
from invoiceops_agent.graph.settings import GraphSettings
from invoiceops_agent.graph.state import GraphState

logger = logging.getLogger(__name__)


def restricted_serializer() -> JsonPlusSerializer:
    """Allow built-in safe types and the one pinned state model; never allow pickle."""
    return JsonPlusSerializer(
        pickle_fallback=False, allowed_msgpack_modules=[GraphState], allowed_json_modules=[]
    )


def _lock_key(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], signed=True)


class InProcessRunLock:
    """Offline seam serializing access to a shared in-memory saver."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self, run_id: UUID, trace_id: str) -> AsyncIterator[None]:
        async with self._lock:
            yield


class PostgresRunLock:
    """One connection owns the session lock until execution and checkpoint writes finish."""

    def __init__(self, connection: psycopg.AsyncConnection[DictRow]) -> None:
        self.connection = connection
        self._local_lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self, run_id: UUID, trace_id: str) -> AsyncIterator[None]:
        async with self._local_lock:
            key = _lock_key(f"invoiceops:graph:run:{run_id}")
            acquired: bool | None = None
            try:
                cursor = await self.connection.execute(
                    "SELECT pg_try_advisory_lock(%s) AS acquired", (key,)
                )
                row = await cursor.fetchone()
                if row is None or not isinstance(row.get("acquired"), bool):
                    raise CheckpointUnavailable(
                        "Run lock result unavailable", run_id=run_id, trace_id=trace_id
                    )
                acquired = row["acquired"]
                if not acquired:
                    raise RunInProgress(
                        "Run is already executing", run_id=run_id, trace_id=trace_id
                    )
                yield
            finally:
                if acquired is None:
                    # A cancelled round-trip may have acquired a server-side session lock.
                    await self.connection.close()
                elif acquired:
                    released = False
                    try:
                        await self.connection.execute("SELECT pg_advisory_unlock(%s)", (key,))
                        released = True
                    finally:
                        if not released:
                            await self.connection.close()


@asynccontextmanager
async def postgres_graph(
    settings: GraphSettings, *, nodes: HelloNodes | None = None
) -> AsyncIterator[GraphRunner]:
    """Create only LangGraph's isolated tables and close the connection on every exit."""
    try:
        connection = await psycopg.AsyncConnection.connect(
            settings.checkpoint_dsn.get_secret_value(),
            autocommit=True,
            prepare_threshold=0,
            row_factory=dict_row,
            connect_timeout=5,
            options="-c search_path=langgraph -c statement_timeout=5000",
        )
    except psycopg.Error as error:
        raise CheckpointUnavailable("Checkpoint connection failed") from error
    async with connection:
        saver = AsyncPostgresSaver(connection, serde=restricted_serializer())
        setup_key = _lock_key("invoiceops:langgraph:setup")
        try:
            await connection.execute("SELECT pg_advisory_lock(%s)", (setup_key,))
            try:
                await connection.execute("CREATE SCHEMA IF NOT EXISTS langgraph")
                await saver.setup()
            finally:
                await connection.execute("SELECT pg_advisory_unlock(%s)", (setup_key,))
        except psycopg.Error as error:
            raise CheckpointUnavailable("Checkpoint setup failed") from error
        logger.info("checkpoint_store_ready schema=langgraph graph_version=hello-v1")
        yield GraphRunner(
            build_hello_graph(saver, nodes=nodes),
            PostgresRunLock(connection),
            timeout_seconds=settings.graph_timeout_seconds,
        )

"""Deterministic ownership checks for interrupted PostgreSQL advisory locks."""

import asyncio
from typing import Literal, cast
from unittest.mock import AsyncMock
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import DictRow

from invoiceops_agent.graph.checkpoints import PostgresRunLock
from invoiceops_agent.graph.errors import CheckpointUnavailable, RunInProgress

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
RUN_ID = UUID("00000000-0000-4000-8000-000000000201")
TRACE_ID = "1234567890abcdef1234567890abcdef"


@pytest.mark.parametrize("cancel_during", ["execute", "fetchone"])
async def test_cancelled_acquisition_discards_uncertain_session(
    cancel_during: Literal["execute", "fetchone"],
) -> None:
    connection = AsyncMock(spec=psycopg.AsyncConnection)
    cursor = AsyncMock(spec=psycopg.AsyncCursor)
    reached = asyncio.Event()
    session_owns_lock = False

    async def execute(query: str, parameters: tuple[int, ...]) -> AsyncMock:
        nonlocal session_owns_lock
        assert "pg_try_advisory_lock" in query
        session_owns_lock = True
        if cancel_during == "execute":
            reached.set()
            await asyncio.Event().wait()
        return cursor

    async def fetchone() -> dict[str, bool]:
        if cancel_during == "fetchone":
            reached.set()
            await asyncio.Event().wait()
        return {"acquired": True}

    async def close() -> None:
        nonlocal session_owns_lock
        session_owns_lock = False

    connection.execute.side_effect = execute
    connection.close.side_effect = close
    cursor.fetchone.side_effect = fetchone
    lock = PostgresRunLock(cast(psycopg.AsyncConnection[DictRow], connection))

    async def acquire() -> None:
        async with lock.acquire(RUN_ID, TRACE_ID):
            pytest.fail("Cancelled acquisition must not enter the protected body")

    task = asyncio.create_task(acquire())
    await reached.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not session_owns_lock
    connection.close.assert_awaited_once()
    assert connection.execute.await_count == 1


async def test_known_contention_keeps_session_without_unlocking() -> None:
    connection = AsyncMock(spec=psycopg.AsyncConnection)
    connection.execute.return_value.fetchone.return_value = {"acquired": False}
    lock = PostgresRunLock(cast(psycopg.AsyncConnection[DictRow], connection))

    with pytest.raises(RunInProgress):
        async with lock.acquire(RUN_ID, TRACE_ID):
            pytest.fail("Contended acquisition must not enter the protected body")

    connection.close.assert_not_awaited()
    assert connection.execute.await_count == 1


async def test_confirmed_acquisition_unlocks_and_preserves_session_after_failure() -> None:
    connection = AsyncMock(spec=psycopg.AsyncConnection)
    connection.execute.return_value.fetchone.return_value = {"acquired": True}
    lock = PostgresRunLock(cast(psycopg.AsyncConnection[DictRow], connection))

    with pytest.raises(RuntimeError, match="node failed"):
        async with lock.acquire(RUN_ID, TRACE_ID):
            raise RuntimeError("node failed")

    connection.close.assert_not_awaited()
    assert connection.execute.await_count == 2
    assert "pg_advisory_unlock" in connection.execute.await_args_list[-1].args[0]


async def test_cancelled_unlock_discards_uncertain_session() -> None:
    connection = AsyncMock(spec=psycopg.AsyncConnection)
    cursor = AsyncMock(spec=psycopg.AsyncCursor)
    cursor.fetchone.return_value = {"acquired": True}
    reached_unlock = asyncio.Event()
    session_owns_lock = False

    async def execute(query: str, parameters: tuple[int, ...]) -> AsyncMock:
        nonlocal session_owns_lock
        if "pg_try_advisory_lock" in query:
            session_owns_lock = True
            return cursor
        reached_unlock.set()
        await asyncio.Event().wait()
        return cursor

    async def close() -> None:
        nonlocal session_owns_lock
        session_owns_lock = False

    connection.execute.side_effect = execute
    connection.close.side_effect = close
    lock = PostgresRunLock(cast(psycopg.AsyncConnection[DictRow], connection))

    async def acquire_and_release() -> None:
        async with lock.acquire(RUN_ID, TRACE_ID):
            pass

    task = asyncio.create_task(acquire_and_release())
    await reached_unlock.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not session_owns_lock
    connection.close.assert_awaited_once()
    assert connection.execute.await_count == 2


@pytest.mark.parametrize("row", [None, {"acquired": None}])
async def test_unknown_lock_result_discards_session(row: dict[str, None] | None) -> None:
    connection = AsyncMock(spec=psycopg.AsyncConnection)
    connection.execute.return_value.fetchone.return_value = row
    lock = PostgresRunLock(cast(psycopg.AsyncConnection[DictRow], connection))

    with pytest.raises(CheckpointUnavailable):
        async with lock.acquire(RUN_ID, TRACE_ID):
            pytest.fail("An uncertain acquisition must not enter the protected body")

    connection.close.assert_awaited_once()
    assert connection.execute.await_count == 1

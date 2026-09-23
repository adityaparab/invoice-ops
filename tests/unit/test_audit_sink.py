"""Short owned audit transactions commit before returning and roll back interrupted work."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import psycopg
import pytest
from psycopg.pq import TransactionStatus
from tests.unit.test_ledger import (
    CREATED_AT,
    EVENT_ID,
    TRACE_ID,
    FakeConnection,
    FakeInfo,
    command,
    identity,
    settings,
)

from invoiceops_agent.ledger.audit import AuditSettings, TransactionalAuditSink
from invoiceops_agent.ledger.connection import LedgerConnection
from invoiceops_agent.ledger.errors import LedgerStorageError, LedgerTransactionRequired
from invoiceops_agent.ledger.schemas import AppendEvent, LedgerEvent
from invoiceops_agent.ledger.writer import LedgerWriter

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@dataclass
class TransactionConnection(FakeConnection):
    commit_failure: bool = False
    committed: bool = False
    rolled_back: bool = False
    closed: bool = False

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[object]:
        self.info.transaction_status = TransactionStatus.INTRANS
        try:
            yield self
            if self.commit_failure:
                raise psycopg.OperationalError("synthetic-private-commit-detail")
        except (Exception, asyncio.CancelledError):
            self.rolled_back = True
            raise
        else:
            self.committed = True
        finally:
            self.info.transaction_status = TransactionStatus.IDLE

    @asynccontextmanager
    async def connection(self) -> AsyncIterator["TransactionConnection"]:
        try:
            yield self
        finally:
            self.closed = True


@pytest.mark.parametrize("fail_commit", [False, True])
async def test_owned_audit_transaction_returns_only_committed_events(fail_commit: bool) -> None:
    connection = TransactionConnection(
        [[identity()], [{"sequence": 1}], []],
        info=FakeInfo(TransactionStatus.IDLE),
        commit_failure=fail_commit,
    )
    writer = LedgerWriter(settings(), clock=lambda: CREATED_AT, new_id=lambda: EVENT_ID)
    sink = TransactionalAuditSink(connection.connection, writer)
    if fail_commit:
        with pytest.raises(LedgerStorageError) as error:
            await sink.append(command(), trace_id=TRACE_ID)
        assert "synthetic-private-commit-detail" not in str(error.value)
        assert connection.rolled_back and not connection.committed
    else:
        result = await sink.append(command(), trace_id=TRACE_ID)
        assert result.id == EVENT_ID and connection.committed
    assert connection.closed


async def test_sink_rejects_existing_transaction_without_claiming_nested_commit() -> None:
    connection = TransactionConnection([])
    with pytest.raises(LedgerTransactionRequired):
        await TransactionalAuditSink(connection.connection, LedgerWriter(settings())).append(
            command(), trace_id=TRACE_ID
        )
    assert not connection.calls and not connection.committed


@pytest.mark.parametrize("interrupt", ["deadline", "cancel"])
async def test_interrupted_audit_rolls_back_and_cancellation_propagates(interrupt: str) -> None:
    entered = asyncio.Event()

    class BlockedWriter:
        async def append(
            self, connection: LedgerConnection, command: AppendEvent, *, trace_id: str
        ) -> LedgerEvent:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("Blocked writer must be interrupted")

    connection = TransactionConnection([], info=FakeInfo(TransactionStatus.IDLE))
    sink = TransactionalAuditSink(
        connection.connection,
        BlockedWriter(),
        AuditSettings(timeout_seconds=0.02),
    )
    pending = asyncio.create_task(sink.append(command(), trace_id=TRACE_ID))
    await entered.wait()
    if interrupt == "cancel":
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    else:
        with pytest.raises(LedgerStorageError):
            await pending
    assert connection.closed and connection.rolled_back and not connection.committed

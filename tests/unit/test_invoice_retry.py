"""Retry only infrastructure failures and keep business outcomes single-attempt."""

import asyncio
from decimal import Decimal
from uuid import UUID

import psycopg
import pytest
from pydantic import ValidationError
from tests.unit.test_invoice_graph import _state

from invoiceops_agent.graph.errors import GraphExecutionError
from invoiceops_agent.graph.retry import RetryConfig, is_infrastructure_error
from invoiceops_agent.graph.state import InvoiceGraphState
from invoiceops_agent.graph.worker import RetryWorker, RunAttempt

pytestmark = pytest.mark.unit
RUN_ID = UUID(int=810)


class FakeStore:
    def __init__(self) -> None:
        self.failures: list[tuple[int, str, bool, bool]] = []
        self.settled = False
        self.continuations: list[bool] = []

    async def begin(self, run_id: UUID, *, continuation: bool = False) -> RunAttempt:
        self.continuations.append(continuation)
        state = _state()
        return RunAttempt(
            run_id=run_id,
            invoice_id=state.invoice_id,
            trace_id=state.trace_id,
            graph_version="invoice-v1",
            attempt=len(self.failures) + 1,
            cycle=0,
        )

    async def settle(self, attempt: RunAttempt, result: InvoiceGraphState) -> None:
        self.settled = True

    async def failed(
        self, attempt: RunAttempt, *, error_type: str, retryable: bool, terminal: bool
    ) -> None:
        self.failures.append((attempt.attempt, error_type, retryable, terminal))


@pytest.mark.asyncio
async def test_infrastructure_errors_back_off_and_resume_same_run() -> None:
    store = FakeStore()
    calls = 0
    delays: list[float] = []

    async def run_once(run_id: UUID) -> InvoiceGraphState:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise GraphExecutionError("sanitized", run_id=run_id) from psycopg.OperationalError()
        return _state()

    async def sleep(delay: float) -> None:
        delays.append(delay)

    worker = RetryWorker(
        store,
        run_once,
        RetryConfig(initial_delay_seconds=Decimal("0.25"), max_delay_seconds=Decimal("1")),
        sleep=sleep,
    )
    assert await worker.process(RUN_ID) == _state()
    assert calls == 3
    assert store.failures == [
        (1, "OperationalError", True, False),
        (2, "OperationalError", True, False),
    ]
    assert store.continuations == [False, True, True]
    assert delays == [0.25, 0.5]
    assert store.settled


@pytest.mark.asyncio
async def test_business_failure_is_dead_lettered_without_retry() -> None:
    store = FakeStore()
    calls = 0

    async def run_once(run_id: UUID) -> InvoiceGraphState:
        nonlocal calls
        calls += 1
        raise GraphExecutionError("sanitized", run_id=run_id) from ValueError("business")

    async def sleep(delay: float) -> None:
        pytest.fail("Business failure must not sleep or retry")

    with pytest.raises(GraphExecutionError):
        await RetryWorker(store, run_once, sleep=sleep).process(RUN_ID)
    assert calls == 1
    assert store.failures == [(1, "ValueError", False, True)]


@pytest.mark.asyncio
async def test_exhausted_infrastructure_budget_dead_letters_after_three_attempts() -> None:
    store = FakeStore()

    async def run_once(run_id: UUID) -> InvoiceGraphState:
        raise psycopg.OperationalError()

    async def sleep(delay: float) -> None:
        pass

    with pytest.raises(psycopg.OperationalError):
        await RetryWorker(store, run_once, sleep=sleep).process(RUN_ID)
    assert store.failures[-1] == (3, "OperationalError", True, True)
    assert len(store.failures) == 3


@pytest.mark.asyncio
async def test_cancellation_preserves_retry_state() -> None:
    store = FakeStore()

    async def run_once(run_id: UUID) -> InvoiceGraphState:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await RetryWorker(store, run_once).process(RUN_ID)
    assert store.failures == []


def test_retry_config_and_classification() -> None:
    config = RetryConfig(initial_delay_seconds=Decimal("0.5"), max_delay_seconds=Decimal("2"))
    assert [config.delay(i) for i in range(1, 5)] == [0.5, 1.0, 2.0, 2.0]
    assert is_infrastructure_error(psycopg.OperationalError())
    assert not is_infrastructure_error(ValueError("business"))
    with pytest.raises(ValidationError, match="Maximum retry delay"):
        RetryConfig(initial_delay_seconds=Decimal(3), max_delay_seconds=Decimal(2))

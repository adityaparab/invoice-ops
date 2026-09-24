"""Deterministic hello execution, recovery, and serialization with in-memory checkpoints."""

import asyncio
import logging
from uuid import UUID

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, SecretStr, ValidationError

from invoiceops_agent.graph.checkpoints import InProcessRunLock, restricted_serializer
from invoiceops_agent.graph.errors import GraphExecutionError, GraphTimeout, RunConflict
from invoiceops_agent.graph.hello import build_hello_graph
from invoiceops_agent.graph.nodes.hello import HelloNodes, hello_finish, hello_start
from invoiceops_agent.graph.runner import GraphRunner
from invoiceops_agent.graph.settings import GraphSettings
from invoiceops_agent.graph.state import GraphState, StateUpdate

pytestmark = pytest.mark.unit
RUN_ID = UUID("00000000-0000-4000-8000-000000000001")
INVOICE_ID = UUID("00000000-0000-4000-8000-000000000002")
TRACE_ID = "1234567890abcdef1234567890abcdef"


def make_runner(saver: InMemorySaver, nodes: HelloNodes | None = None) -> GraphRunner:
    return GraphRunner(build_hello_graph(saver, nodes=nodes), InProcessRunLock())


@pytest.mark.asyncio
async def test_hello_persists_every_stub_and_replays_without_execution(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    saver = InMemorySaver(serde=restricted_serializer())
    runner = make_runner(saver)
    result = await runner.run(run_id=RUN_ID, invoice_id=INVOICE_ID, trace_id=TRACE_ID)
    assert result.status == "completed"
    assert result.workflow == "hello-stubs"
    assert result.completed_nodes == ["hello_start", "hello_finish"]
    assert result.trace_id == TRACE_ID
    snapshots = [
        snapshot
        async for snapshot in runner.graph.aget_state_history(
            {"configurable": {"thread_id": str(RUN_ID)}}
        )
    ]
    statuses = [snapshot.values.get("status") for snapshot in snapshots]
    assert statuses[:3] == ["completed", "running", "queued"]

    async def must_not_run(state: GraphState) -> StateUpdate:
        raise AssertionError("A completed run must be served from its checkpoint")

    restarted = make_runner(saver, HelloNodes(must_not_run, must_not_run))
    replayed = await restarted.run(run_id=RUN_ID, invoice_id=INVOICE_ID)
    assert replayed == result
    assert f"run_id={RUN_ID}" in caplog.text
    assert f"trace_id={TRACE_ID}" in caplog.text
    assert "graph_replayed" in caplog.text
    assert "duration_ms=" in caplog.text


@pytest.mark.asyncio
async def test_failed_node_resumes_without_replaying_completed_stub() -> None:
    saver = InMemorySaver(serde=restricted_serializer())
    calls: list[str] = []

    async def start(state: GraphState) -> StateUpdate:
        calls.append("start")
        return await hello_start(state)

    async def fail(state: GraphState) -> StateUpdate:
        calls.append("failed-finish")
        raise RuntimeError("simulated failure")

    first = make_runner(saver, HelloNodes(start, fail))
    with pytest.raises(GraphExecutionError):
        await first.run(run_id=RUN_ID, invoice_id=INVOICE_ID)
    assert calls == ["start", "failed-finish"]

    async def finish(state: GraphState) -> StateUpdate:
        calls.append("resumed-finish")
        return await hello_finish(state)

    restarted = make_runner(saver, HelloNodes(start, finish))
    result = await restarted.run(run_id=RUN_ID, invoice_id=INVOICE_ID)
    assert result.status == "completed"
    assert calls == ["start", "failed-finish", "resumed-finish"]


@pytest.mark.asyncio
async def test_run_id_cannot_be_reused_for_another_invoice() -> None:
    runner = make_runner(InMemorySaver(serde=restricted_serializer()))
    await runner.run(run_id=RUN_ID, invoice_id=INVOICE_ID)
    with pytest.raises(RunConflict) as error:
        await runner.run(run_id=RUN_ID, invoice_id=UUID(int=3), trace_id=TRACE_ID)
    assert error.value.run_id == RUN_ID
    assert error.value.trace_id == TRACE_ID


@pytest.mark.asyncio
async def test_concurrent_local_calls_execute_stubs_once() -> None:
    calls: list[str] = []

    async def start(state: GraphState) -> StateUpdate:
        calls.append("start")
        await asyncio.sleep(0)
        return await hello_start(state)

    runner = make_runner(
        InMemorySaver(serde=restricted_serializer()), HelloNodes(start, hello_finish)
    )
    first, second = await asyncio.gather(
        runner.run(run_id=RUN_ID, invoice_id=INVOICE_ID),
        runner.run(run_id=RUN_ID, invoice_id=INVOICE_ID),
    )
    assert first == second
    assert calls == ["start"]


@pytest.mark.asyncio
async def test_failure_messages_and_logs_omit_sensitive_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail(state: GraphState) -> StateUpdate:
        raise RuntimeError("private-connection-secret")

    runner = make_runner(
        InMemorySaver(serde=restricted_serializer()), HelloNodes(fail, hello_finish)
    )
    with pytest.raises(GraphExecutionError) as error:
        await runner.run(run_id=RUN_ID, invoice_id=INVOICE_ID, trace_id=TRACE_ID)
    assert "private-connection-secret" not in str(error.value) + caplog.text
    assert f"run_id={RUN_ID}" in str(error.value)
    assert f"trace_id={TRACE_ID}" in caplog.text


@pytest.mark.asyncio
async def test_timeout_cancels_node_and_permits_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled = asyncio.Event()
    entered = asyncio.Event()
    deadline = asyncio.timeout(None)

    def controlled_timeout(seconds: float) -> asyncio.Timeout:
        return deadline

    monkeypatch.setattr("invoiceops_agent.graph.runner.timeout", controlled_timeout)

    async def blocked(state: GraphState) -> StateUpdate:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return await hello_finish(state)

    saver = InMemorySaver(serde=restricted_serializer())
    runner = GraphRunner(
        build_hello_graph(saver, nodes=HelloNodes(hello_start, blocked)),
        InProcessRunLock(),
    )
    task = asyncio.create_task(runner.run(run_id=RUN_ID, invoice_id=INVOICE_ID))
    await entered.wait()
    deadline.reschedule(asyncio.get_running_loop().time())
    with pytest.raises(GraphTimeout):
        await task
    assert cancelled.is_set()
    monkeypatch.undo()
    resumed = await make_runner(saver).run(run_id=RUN_ID, invoice_id=INVOICE_ID)
    assert resumed.status == "completed"


def test_state_rejects_inconsistent_progress() -> None:
    with pytest.raises(ValidationError):
        GraphState(run_id=RUN_ID, invoice_id=INVOICE_ID, trace_id=TRACE_ID, status="completed")


def test_settings_require_dsn_and_redact_bad_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INVOICEOPS_CHECKPOINT_DSN", raising=False)
    with pytest.raises(ValidationError):
        GraphSettings()
    monkeypatch.setenv("INVOICEOPS_CHECKPOINT_DSN", "mysql://user:private-secret@host/db")
    with pytest.raises(ValidationError) as error:
        GraphSettings()
    assert "private-secret" not in str(error.value)
    monkeypatch.setenv("INVOICEOPS_CHECKPOINT_DSN", "postgresql://user:private-secret@host/db")
    settings = GraphSettings()
    assert settings.checkpoint_dsn.get_secret_value().startswith("postgresql://")
    assert "private-secret" not in repr(settings)


@pytest.mark.parametrize("timeout", [0, -1, 301, float("inf"), float("nan")])
def test_graph_timeout_is_bounded(timeout: float) -> None:
    with pytest.raises(ValidationError):
        GraphSettings(
            checkpoint_dsn=SecretStr("postgresql://host/db"), graph_timeout_seconds=timeout
        )


def test_invoice_graph_budget_covers_three_bounded_model_calls() -> None:
    settings = GraphSettings(checkpoint_dsn=SecretStr("postgresql://host/db"))
    assert settings.invoice_graph_timeout_seconds >= 3 * 120 + 30


@pytest.mark.parametrize("timeout", [0, -1, 451, float("inf"), float("nan")])
def test_invoice_graph_deadline_stays_below_the_running_lease(timeout: float) -> None:
    with pytest.raises(ValidationError):
        GraphSettings(
            checkpoint_dsn=SecretStr("postgresql://host/db"),
            invoice_graph_timeout_seconds=timeout,
        )


def test_restricted_serializer_round_trips_only_allowed_state() -> None:
    state = GraphState(run_id=RUN_ID, invoice_id=INVOICE_ID, trace_id=TRACE_ID)
    serializer = restricted_serializer()
    encoded = serializer.dumps_typed(state)
    assert encoded[0] == "msgpack"
    assert serializer.loads_typed(encoded) == state
    with pytest.raises(NotImplementedError):
        serializer.loads_typed(("pickle", b"not-a-pickle"))


class UnregisteredModel(BaseModel):
    value: str


def test_serializer_does_not_reconstruct_unregistered_models() -> None:
    serializer = restricted_serializer()
    encoded = serializer.dumps_typed(UnregisteredModel(value="hello"))
    decoded = serializer.loads_typed(encoded)
    assert not isinstance(decoded, UnregisteredModel)

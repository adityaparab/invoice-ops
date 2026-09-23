"""Real Postgres checkpoints survive connection restarts and isolate public tables."""

import asyncio
from urllib.parse import quote
from uuid import UUID

import psycopg
import pytest
from pydantic import SecretStr

from invoiceops_agent.graph.checkpoints import PostgresRunLock, postgres_graph
from invoiceops_agent.graph.errors import GraphExecutionError, RunConflict, RunInProgress
from invoiceops_agent.graph.nodes.hello import HelloNodes, hello_finish, hello_start
from invoiceops_agent.graph.settings import GraphSettings
from invoiceops_agent.graph.state import GraphState, StateUpdate

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
RUN_ID = UUID("00000000-0000-4000-8000-000000000011")
INVOICE_ID = UUID("00000000-0000-4000-8000-000000000012")


@pytest.fixture
def checkpoint_settings(
    postgres_connection: psycopg.Connection[tuple[object, ...]],
) -> GraphSettings:
    info = postgres_connection.info
    dsn = (
        f"postgresql://{quote(info.user, safe='')}:{quote(info.password, safe='')}"
        f"@{info.host}:{info.port}/{quote(info.dbname, safe='')}"
    )
    return GraphSettings(checkpoint_dsn=SecretStr(dsn))


async def test_postgres_restart_replays_and_preserves_public_tables(
    checkpoint_settings: GraphSettings,
) -> None:
    async with await psycopg.AsyncConnection.connect(
        checkpoint_settings.checkpoint_dsn.get_secret_value(), autocommit=True
    ) as connection:
        await connection.execute("CREATE TABLE public.checkpoints (sentinel text)")
        await connection.execute("INSERT INTO public.checkpoints VALUES ('untouched')")
        async with postgres_graph(checkpoint_settings) as runner:
            completed = await runner.run(run_id=RUN_ID, invoice_id=INVOICE_ID)
            assert isinstance(runner.lock, PostgresRunLock)
            graph_connection = runner.lock.connection
            snapshots = [
                checkpoint
                async for checkpoint in runner.graph.aget_state_history(
                    {"configurable": {"thread_id": str(RUN_ID)}}
                )
            ]
            assert [item.values.get("status") for item in snapshots][:3] == [
                "completed",
                "running",
                "queued",
            ]
        assert graph_connection.closed

        async def fail_if_executed(state: GraphState) -> StateUpdate:
            raise AssertionError("Replay must not execute stubs")

        async with postgres_graph(
            checkpoint_settings, nodes=HelloNodes(fail_if_executed, fail_if_executed)
        ) as restarted:
            assert await restarted.run(run_id=RUN_ID, invoice_id=INVOICE_ID) == completed
            with pytest.raises(RunConflict):
                await restarted.run(run_id=RUN_ID, invoice_id=UUID(int=13))
        cursor = await connection.execute("SELECT sentinel FROM public.checkpoints")
        assert await cursor.fetchone() == ("untouched",)
        cursor = await connection.execute(
            "SELECT count(*) FROM langgraph.checkpoints WHERE thread_id = %s", (str(RUN_ID),)
        )
        count = await cursor.fetchone()
        assert count is not None and count[0] >= 4


async def test_postgres_resume_starts_at_failed_stub(checkpoint_settings: GraphSettings) -> None:
    calls: list[str] = []

    async def start(state: GraphState) -> StateUpdate:
        calls.append("start")
        return await hello_start(state)

    async def fail(state: GraphState) -> StateUpdate:
        raise RuntimeError("simulated interruption")

    async with postgres_graph(checkpoint_settings, nodes=HelloNodes(start, fail)) as first:
        with pytest.raises(GraphExecutionError):
            await first.run(run_id=RUN_ID, invoice_id=INVOICE_ID)

    async with postgres_graph(
        checkpoint_settings, nodes=HelloNodes(start, hello_finish)
    ) as restarted:
        result = await restarted.run(run_id=RUN_ID, invoice_id=INVOICE_ID)
    assert result.status == "completed"
    assert calls == ["start"]


async def test_independent_connections_cannot_execute_the_same_run_concurrently(
    checkpoint_settings: GraphSettings,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def waiting(state: GraphState) -> StateUpdate:
        entered.set()
        await release.wait()
        return await hello_start(state)

    async with (
        postgres_graph(checkpoint_settings, nodes=HelloNodes(waiting, hello_finish)) as first,
        postgres_graph(checkpoint_settings) as second,
    ):
        task = asyncio.create_task(first.run(run_id=RUN_ID, invoice_id=INVOICE_ID))
        try:
            await entered.wait()
            with pytest.raises(RunInProgress):
                await second.run(run_id=RUN_ID, invoice_id=INVOICE_ID)
        finally:
            release.set()
            completed = await task
        assert await second.run(run_id=RUN_ID, invoice_id=INVOICE_ID) == completed

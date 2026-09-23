"""CLI results and configuration failures stay structured and offline."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import psycopg
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr

from invoiceops_agent.graph import cli
from invoiceops_agent.graph.checkpoints import (
    InProcessRunLock,
    postgres_graph,
    restricted_serializer,
)
from invoiceops_agent.graph.errors import CheckpointUnavailable
from invoiceops_agent.graph.hello import build_hello_graph
from invoiceops_agent.graph.runner import GraphRunner
from invoiceops_agent.graph.settings import GraphSettings
from invoiceops_agent.graph.state import GraphState

pytestmark = pytest.mark.unit
RUN_ID = UUID("00000000-0000-4000-8000-000000000101")
INVOICE_ID = UUID("00000000-0000-4000-8000-000000000102")


@pytest.mark.asyncio
async def test_demo_logs_a_validated_state_and_closes_runtime(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    events: list[str] = []
    runner = GraphRunner(
        build_hello_graph(InMemorySaver(serde=restricted_serializer())), InProcessRunLock()
    )

    @asynccontextmanager
    async def runtime(settings: GraphSettings) -> AsyncIterator[GraphRunner]:
        events.append("opened")
        try:
            yield runner
        finally:
            events.append("closed")

    monkeypatch.setattr(cli, "postgres_graph", runtime)
    monkeypatch.setenv("INVOICEOPS_CHECKPOINT_DSN", "postgresql://offline/example")
    assert await cli.run_demo(run_id=RUN_ID, invoice_id=INVOICE_ID) == 0
    assert events == ["opened", "closed"]
    result_message = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("graph_demo_result state=")
    )
    state = GraphState.model_validate_json(result_message.removeprefix("graph_demo_result state="))
    assert state.run_id == RUN_ID
    assert state.invoice_id == INVOICE_ID
    assert state.status == "completed"


@pytest.mark.asyncio
async def test_demo_fails_cleanly_without_config(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("INVOICEOPS_CHECKPOINT_DSN", raising=False)
    assert await cli.run_demo(run_id=RUN_ID, invoice_id=INVOICE_ID) == 1
    assert "error_type=ValidationError" in caplog.text
    assert f"run_id={RUN_ID}" in caplog.text


@pytest.mark.asyncio
async def test_postgres_connection_failure_is_typed_and_cli_omits_secret_details(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def fail(*args: object, **kwargs: object) -> None:
        raise psycopg.OperationalError("sensitive-connection-secret")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", fail)
    settings = GraphSettings(checkpoint_dsn=SecretStr("postgresql://offline/example"))
    with pytest.raises(CheckpointUnavailable) as error:
        async with postgres_graph(settings):
            raise AssertionError("The context manager must not yield")
    assert "sensitive-connection-secret" not in str(error.value)
    monkeypatch.setenv("INVOICEOPS_CHECKPOINT_DSN", "postgresql://offline/example")
    assert await cli.run_demo(run_id=RUN_ID, invoice_id=INVOICE_ID) == 1
    assert "sensitive-connection-secret" not in caplog.text
    assert "error_type=CheckpointUnavailable" in caplog.text


def test_entrypoint_preserves_explicit_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[tuple[UUID, UUID]] = []

    async def demo(*, run_id: UUID, invoice_id: UUID) -> int:
        received.append((run_id, invoice_id))
        return 0

    monkeypatch.setattr(cli, "run_demo", demo)
    monkeypatch.setattr(
        "sys.argv",
        ["invoiceops-graph-demo", "--run-id", str(RUN_ID), "--invoice-id", str(INVOICE_ID)],
    )
    assert cli.main() == 0
    assert received == [(RUN_ID, INVOICE_ID)]


def test_entrypoint_creates_ids_when_unspecified(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[tuple[UUID, UUID]] = []

    async def demo(*, run_id: UUID, invoice_id: UUID) -> int:
        received.append((run_id, invoice_id))
        return 0

    monkeypatch.setattr(cli, "run_demo", demo)
    monkeypatch.setattr("sys.argv", ["invoiceops-graph-demo"])
    assert cli.main() == 0
    assert len(received) == 1
    assert all(value.version == 4 for value in received[0])

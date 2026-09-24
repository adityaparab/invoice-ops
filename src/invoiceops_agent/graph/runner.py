"""Idempotent, resumable orchestration around the injected compiled hello graph."""

import logging
from asyncio import timeout
from contextlib import AbstractAsyncContextManager
from time import perf_counter
from typing import Protocol
from uuid import UUID, uuid4

from langchain_core.runnables import RunnableConfig
from pydantic import ValidationError

from invoiceops_agent.graph.errors import (
    GraphError,
    GraphExecutionError,
    GraphTimeout,
    InvalidCheckpoint,
    RunConflict,
)
from invoiceops_agent.graph.hello import HelloGraph
from invoiceops_agent.graph.state import GraphState
from invoiceops_agent.obs.tracing import operation_span

logger = logging.getLogger(__name__)


class RunLock(Protocol):
    def acquire(self, run_id: UUID, trace_id: str) -> AbstractAsyncContextManager[None]: ...


class GraphRunner:
    def __init__(self, graph: HelloGraph, lock: RunLock, *, timeout_seconds: float = 30) -> None:
        self.graph = graph
        self.lock = lock
        self.timeout_seconds = timeout_seconds

    async def run(
        self, *, run_id: UUID, invoice_id: UUID, trace_id: str | None = None
    ) -> GraphState:
        trace = trace_id if trace_id is not None else uuid4().hex
        initial = GraphState(run_id=run_id, invoice_id=invoice_id, trace_id=trace)
        started = perf_counter()
        try:
            async with timeout(self.timeout_seconds), self.lock.acquire(run_id, trace):
                with operation_span("workflow", "hello", initial):
                    result = await self._run_locked(initial)
        except GraphError as error:
            logger.warning(
                "graph_rejected run_id=%s trace_id=%s error_type=%s",
                run_id,
                trace,
                type(error).__name__,
            )
            raise
        except TimeoutError as error:
            logger.warning("graph_timeout run_id=%s trace_id=%s", run_id, trace)
            raise GraphTimeout(
                "Graph execution timed out", run_id=run_id, trace_id=trace
            ) from error
        except Exception as error:
            logger.error(
                "graph_failed run_id=%s trace_id=%s error_type=%s",
                run_id,
                trace,
                type(error).__name__,
            )
            raise GraphExecutionError(
                "Graph execution failed", run_id=run_id, trace_id=trace
            ) from error
        logger.info(
            "graph_completed run_id=%s trace_id=%s duration_ms=%.3f workflow=hello-stubs",
            run_id,
            result.trace_id,
            (perf_counter() - started) * 1000,
        )
        return result

    async def _run_locked(self, initial: GraphState) -> GraphState:
        config: RunnableConfig = {
            "configurable": {"thread_id": str(initial.run_id)},
            "metadata": {"invoice_id": str(initial.invoice_id), "graph_version": "hello-v1"},
        }
        snapshot = await self.graph.aget_state(config)
        exists = snapshot.created_at is not None
        if exists:
            if (snapshot.metadata or {}).get("invoice_id") != str(initial.invoice_id):
                raise RunConflict(
                    "Run already belongs to another invoice",
                    run_id=initial.run_id,
                    trace_id=initial.trace_id,
                )
            if snapshot.values:
                persisted = self._validated_state(snapshot.values, initial)
                if persisted.run_id != initial.run_id or persisted.invoice_id != initial.invoice_id:
                    raise InvalidCheckpoint(
                        "Checkpoint identity is inconsistent",
                        run_id=initial.run_id,
                        trace_id=initial.trace_id,
                    )
                if persisted.status == "completed" and not snapshot.next:
                    logger.info(
                        "graph_replayed run_id=%s trace_id=%s", persisted.run_id, persisted.trace_id
                    )
                    return persisted
            logger.info("graph_resuming run_id=%s trace_id=%s", initial.run_id, initial.trace_id)
        # Passing None resumes pending tasks; supplying initial state again would restart the graph.
        output = await self.graph.ainvoke(None if exists else initial, config, durability="sync")
        result = self._validated_state(output, initial)
        if result.status != "completed":
            raise InvalidCheckpoint(
                "Hello graph did not finish", run_id=initial.run_id, trace_id=initial.trace_id
            )
        return result

    @staticmethod
    def _validated_state(value: object, initial: GraphState) -> GraphState:
        try:
            return GraphState.model_validate(value)
        except ValidationError as error:
            raise InvalidCheckpoint(
                "Checkpoint state is incompatible with hello-v1",
                run_id=initial.run_id,
                trace_id=initial.trace_id,
            ) from error

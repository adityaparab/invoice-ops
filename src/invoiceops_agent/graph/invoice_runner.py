"""Replay-safe entry and human-review resume for the durable invoice graph."""

import logging
from asyncio import timeout
from time import perf_counter
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from pydantic import ValidationError

from invoiceops_agent.graph.errors import (
    GraphError,
    GraphExecutionError,
    GraphTimeout,
    InvalidCheckpoint,
    RunConflict,
)
from invoiceops_agent.graph.invoice import InvoiceGraph
from invoiceops_agent.graph.runner import RunLock
from invoiceops_agent.graph.state import InvoiceGraphState, ReviewDecision
from invoiceops_agent.obs.tracing import operation_span

logger = logging.getLogger(__name__)


class InvoiceGraphRunner:
    def __init__(self, graph: InvoiceGraph, lock: RunLock, *, timeout_seconds: float = 120) -> None:
        self.graph = graph
        self.lock = lock
        self.timeout_seconds = timeout_seconds

    async def run(self, initial: InvoiceGraphState) -> InvoiceGraphState:
        started = perf_counter()
        try:
            async with (
                timeout(self.timeout_seconds),
                self.lock.acquire(initial.run_id, initial.trace_id),
            ):
                with operation_span("workflow", "invoice", initial):
                    result = await self._run_locked(initial)
        except GraphError:
            raise
        except TimeoutError as error:
            raise GraphTimeout(
                "Invoice workflow timed out", run_id=initial.run_id, trace_id=initial.trace_id
            ) from error
        except Exception as error:
            logger.error(
                "invoice_graph_failed run_id=%s trace_id=%s error_type=%s",
                initial.run_id,
                initial.trace_id,
                type(error).__name__,
            )
            raise GraphExecutionError(
                "Invoice workflow failed", run_id=initial.run_id, trace_id=initial.trace_id
            ) from error
        logger.info(
            "invoice_graph_yielded run_id=%s trace_id=%s status=%s duration_ms=%.3f",
            result.run_id,
            result.trace_id,
            result.status,
            (perf_counter() - started) * 1000,
        )
        return result

    async def resume(
        self, *, run_id: UUID, invoice_id: UUID, trace_id: str, decision: ReviewDecision
    ) -> InvoiceGraphState:
        try:
            async with timeout(self.timeout_seconds), self.lock.acquire(run_id, trace_id):
                config = self._config(run_id, invoice_id)
                snapshot = await self.graph.aget_state(config)
                if not snapshot.created_at:
                    raise InvalidCheckpoint(
                        "Invoice workflow has no checkpoint", run_id=run_id, trace_id=trace_id
                    )
                state = self._state(snapshot.values, run_id, trace_id)
                self._identity(state, run_id, invoice_id, trace_id)
                expected_review = decision.model_dump(mode="json")
                if state.status == "completed" and state.review == expected_review:
                    return state
                if state.status == "awaiting_review" and "HumanReview" in snapshot.next:
                    command: Command[object] | None = Command(resume=expected_review)
                elif state.status == "running" and state.review == expected_review:
                    command = None
                else:
                    raise InvalidCheckpoint(
                        "Invoice workflow is not waiting for this human decision",
                        run_id=run_id,
                        trace_id=trace_id,
                    )
                with operation_span("workflow", "review_resume", state):
                    output = await self.graph.ainvoke(command, config, durability="sync")
                result = self._state(output, run_id, trace_id)
                if result.status != "completed":
                    raise InvalidCheckpoint(
                        "Human review did not complete the workflow",
                        run_id=run_id,
                        trace_id=trace_id,
                    )
                return result
        except GraphError:
            raise
        except TimeoutError as error:
            raise GraphTimeout(
                "Invoice review timed out", run_id=run_id, trace_id=trace_id
            ) from error
        except Exception as error:
            logger.error(
                "invoice_review_failed run_id=%s trace_id=%s error_type=%s",
                run_id,
                trace_id,
                type(error).__name__,
            )
            raise GraphExecutionError(
                "Invoice review failed", run_id=run_id, trace_id=trace_id
            ) from error

    async def _run_locked(self, initial: InvoiceGraphState) -> InvoiceGraphState:
        config = self._config(initial.run_id, initial.invoice_id)
        snapshot = await self.graph.aget_state(config)
        exists = snapshot.created_at is not None
        if exists:
            state = self._state(snapshot.values, initial.run_id, initial.trace_id)
            self._identity(state, initial.run_id, initial.invoice_id, initial.trace_id)
            if state.status in {"completed", "rejected", "awaiting_review"}:
                return state
        output = await self.graph.ainvoke(
            None if exists else initial,
            config,
            durability="sync",
        )
        return self._state(output, initial.run_id, initial.trace_id)

    @staticmethod
    def _config(run_id: UUID, invoice_id: UUID) -> RunnableConfig:
        return {
            "configurable": {"thread_id": str(run_id)},
            "metadata": {"invoice_id": str(invoice_id), "graph_version": "invoice-v1"},
        }

    @staticmethod
    def _state(value: object, run_id: UUID, trace_id: str) -> InvoiceGraphState:
        if isinstance(value, dict) and "__interrupt__" in value:
            value = {key: item for key, item in value.items() if key != "__interrupt__"}
        try:
            return InvoiceGraphState.model_validate(value)
        except ValidationError as error:
            raise InvalidCheckpoint(
                "Invoice workflow checkpoint is invalid", run_id=run_id, trace_id=trace_id
            ) from error

    @staticmethod
    def _identity(state: InvoiceGraphState, run_id: UUID, invoice_id: UUID, trace_id: str) -> None:
        if state.run_id != run_id or state.invoice_id != invoice_id or state.trace_id != trace_id:
            raise RunConflict(
                "Run checkpoint identity does not match", run_id=run_id, trace_id=trace_id
            )

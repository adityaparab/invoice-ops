"""Durable ADK session runner with the same run and review contract as LangGraph."""

import logging
from asyncio import timeout
from time import perf_counter
from uuid import UUID

from google.adk import Workflow
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, Session
from google.genai import types
from pydantic import ValidationError

from invoiceops_agent.graph.errors import (
    GraphError,
    GraphExecutionError,
    GraphTimeout,
    InvalidCheckpoint,
    RunConflict,
)
from invoiceops_agent.graph.runner import RunLock
from invoiceops_agent.graph.state import InvoiceGraphState, ReviewDecision
from invoiceops_agent.obs.tracing import operation_span

logger = logging.getLogger(__name__)
APP_NAME = "invoiceops_adk_invoice_v1"
USER_ID = "invoiceops"


class AdkInvoiceRunner:
    def __init__(
        self,
        workflow: Workflow,
        sessions: BaseSessionService,
        lock: RunLock,
        *,
        timeout_seconds: float = 420,
    ) -> None:
        self.sessions = sessions
        self.lock = lock
        self.timeout_seconds = timeout_seconds
        self.runner = Runner(app_name=APP_NAME, node=workflow, session_service=sessions)

    async def run(self, initial: InvoiceGraphState) -> InvoiceGraphState:
        started = perf_counter()
        try:
            async with (
                timeout(self.timeout_seconds),
                self.lock.acquire(initial.run_id, initial.trace_id),
            ):
                session = await self._get(initial.run_id)
                if session is None:
                    session = await self.sessions.create_session(
                        app_name=APP_NAME,
                        user_id=USER_ID,
                        session_id=str(initial.run_id),
                        state={"invoice_state": initial.model_dump(mode="json")},
                    )
                state = self._state(session, initial.run_id, initial.trace_id)
                self._identity(state, initial.run_id, initial.invoice_id, initial.trace_id)
                if state.status in {"completed", "rejected", "awaiting_review"}:
                    return state
                with operation_span("workflow", "invoice_adk", state):
                    await self._invoke(
                        initial.run_id,
                        state.trace_id,
                        invocation_id=session.events[-1].invocation_id if session.events else None,
                    )
                result = await self._required_state(initial.run_id, initial.trace_id)
        except GraphError:
            raise
        except TimeoutError as error:
            raise GraphTimeout(
                "ADK invoice workflow timed out",
                run_id=initial.run_id,
                trace_id=initial.trace_id,
            ) from error
        except Exception as error:
            logger.error(
                "adk_invoice_failed run_id=%s trace_id=%s error_type=%s",
                initial.run_id,
                initial.trace_id,
                type(error).__name__,
            )
            raise GraphExecutionError(
                "ADK invoice workflow failed",
                run_id=initial.run_id,
                trace_id=initial.trace_id,
            ) from error
        logger.info(
            "adk_invoice_yielded run_id=%s trace_id=%s status=%s duration_ms=%.3f",
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
                session = await self._get(run_id)
                if session is None:
                    raise InvalidCheckpoint(
                        "ADK invoice workflow has no session", run_id=run_id, trace_id=trace_id
                    )
                state = self._state(session, run_id, trace_id)
                self._identity(state, run_id, invoice_id, trace_id)
                expected = decision.model_dump(mode="json")
                if state.status == "completed" and state.review == expected:
                    return state
                if state.status == "awaiting_review":
                    request = self._review_request(session, run_id, trace_id)
                    message = types.Content(
                        role="user",
                        parts=[
                            types.Part(
                                function_response=types.FunctionResponse(
                                    id=request[0], name="adk_request_input", response=expected
                                )
                            )
                        ],
                    )
                    invocation_id = request[1]
                elif state.status == "running" and state.review == expected:
                    message = None
                    invocation_id = session.events[-1].invocation_id
                else:
                    raise InvalidCheckpoint(
                        "ADK invoice workflow is not waiting for this human decision",
                        run_id=run_id,
                        trace_id=trace_id,
                    )
                with operation_span("workflow", "review_resume_adk", state):
                    await self._invoke(
                        run_id, trace_id, invocation_id=invocation_id, new_message=message
                    )
                result = await self._required_state(run_id, trace_id)
                if result.status != "completed":
                    raise InvalidCheckpoint(
                        "ADK human review did not complete",
                        run_id=run_id,
                        trace_id=trace_id,
                    )
                return result
        except GraphError:
            raise
        except TimeoutError as error:
            raise GraphTimeout(
                "ADK invoice review timed out", run_id=run_id, trace_id=trace_id
            ) from error
        except Exception as error:
            logger.error(
                "adk_review_failed run_id=%s trace_id=%s error_type=%s",
                run_id,
                trace_id,
                type(error).__name__,
            )
            raise GraphExecutionError(
                "ADK invoice review failed", run_id=run_id, trace_id=trace_id
            ) from error

    async def _invoke(
        self,
        run_id: UUID,
        trace_id: str,
        *,
        invocation_id: str | None = None,
        new_message: types.Content | None = None,
    ) -> None:
        async for event in self.runner.run_async(
            user_id=USER_ID,
            session_id=str(run_id),
            invocation_id=invocation_id,
            new_message=new_message,
        ):
            if event.error_code or event.error_message:
                raise GraphExecutionError(
                    "ADK invoice workflow emitted an error", run_id=run_id, trace_id=trace_id
                )

    async def _get(self, run_id: UUID) -> Session | None:
        return await self.sessions.get_session(
            app_name=APP_NAME, user_id=USER_ID, session_id=str(run_id)
        )

    async def _required_state(self, run_id: UUID, trace_id: str) -> InvoiceGraphState:
        session = await self._get(run_id)
        if session is None:
            raise InvalidCheckpoint("ADK session disappeared", run_id=run_id, trace_id=trace_id)
        return self._state(session, run_id, trace_id)

    @staticmethod
    def _state(session: Session, run_id: UUID, trace_id: str) -> InvoiceGraphState:
        try:
            return InvoiceGraphState.model_validate(session.state["invoice_state"])
        except (KeyError, ValidationError) as error:
            raise InvalidCheckpoint(
                "ADK invoice session is invalid", run_id=run_id, trace_id=trace_id
            ) from error

    @staticmethod
    def _identity(state: InvoiceGraphState, run_id: UUID, invoice_id: UUID, trace_id: str) -> None:
        if state.run_id != run_id or state.invoice_id != invoice_id or state.trace_id != trace_id:
            raise RunConflict(
                "ADK session identity does not match", run_id=run_id, trace_id=trace_id
            )

    @staticmethod
    def _review_request(session: Session, run_id: UUID, trace_id: str) -> tuple[str, str]:
        for event in reversed(session.events):
            if event.content is None or event.content.parts is None:
                continue
            for part in event.content.parts:
                call = part.function_call
                if call is not None and call.name == "adk_request_input" and call.id:
                    return call.id, event.invocation_id
        raise InvalidCheckpoint("ADK review request is absent", run_id=run_id, trace_id=trace_id)

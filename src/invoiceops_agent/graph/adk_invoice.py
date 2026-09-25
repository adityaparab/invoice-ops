"""Google ADK graph variant over the shared invoice nodes and state contract."""

from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

from google.adk import Event, Workflow
from google.adk.agents.context import Context
from google.adk.events import RequestInput

from invoiceops_agent.graph.invoice_nodes import InvoiceNodes
from invoiceops_agent.graph.state import InvoiceGraphState, ReviewDecision
from invoiceops_agent.obs.tracing import traced_node

type NodeWork = Callable[[InvoiceGraphState], Awaitable[dict[str, object]]]
type Route = Callable[[InvoiceGraphState], str]


def _state(ctx: Context) -> InvoiceGraphState:
    return InvoiceGraphState.model_validate(ctx.state["invoice_state"])


def _step(name: str, work: NodeWork, route: Route | None = None) -> Callable[..., Any]:
    async def run(ctx: Context) -> Event:
        current = _state(ctx)
        update = await traced_node(name, work)(current)
        result = InvoiceGraphState.model_validate({**current.model_dump(mode="json"), **update})
        payload = result.model_dump(mode="json")
        ctx.state["invoice_state"] = payload
        return Event(output=payload, route=route(result) if route is not None else None)

    run.__name__ = name
    return run


def build_adk_invoice_workflow(nodes: InvoiceNodes) -> Workflow:
    """Preserve the LangGraph topology while using ADK's route and HITL events."""
    ingest = _step("Ingest", nodes.ingest, lambda s: "REJECT" if s.duplicate else "EXTRACT")
    extract = _step("Extract", nodes.extract, lambda s: "VALIDATE" if s.extraction else "TRIAGE")
    validate = _step("Validate", nodes.validate)
    match = _step("Match3Way", nodes.match_three_way)
    policy = _step("Policy", nodes.policy)
    gate = _step("Gate", nodes.gate, lambda s: "AUTO" if s.route == "AUTO_APPROVE" else "TRIAGE")
    approve = _step("AutoApprove", nodes.auto_approve)
    triage = _step("ExceptionTriage", nodes.exception_triage)
    archive = _step("Archive", nodes.archive)
    reject = _step("Reject", nodes.reject)

    async def ask_review(ctx: Context) -> AsyncGenerator[RequestInput, None]:
        current = _state(ctx)
        yield RequestInput(
            message="Review the invoice exception",
            payload={
                "run_id": str(current.run_id),
                "invoice_id": str(current.invoice_id),
                "triage": current.triage,
            },
            response_schema=ReviewDecision,
        )

    async def apply_review(ctx: Context, node_input: dict[str, object]) -> Event:
        current = _state(ctx)
        decision = ReviewDecision.model_validate(node_input)

        async def review_work(state: InvoiceGraphState) -> dict[str, object]:
            return await nodes.apply_review(state, decision)

        update = await traced_node("HumanReview", review_work)(current)
        result = InvoiceGraphState.model_validate({**current.model_dump(mode="json"), **update})
        payload = result.model_dump(mode="json")
        ctx.state["invoice_state"] = payload
        return Event(output=payload)

    return Workflow(
        name="invoiceops_adk_invoice_v1",
        edges=[
            ("START", ingest),
            (ingest, {"REJECT": reject, "EXTRACT": extract}),
            (extract, {"VALIDATE": validate, "TRIAGE": triage}),
            (validate, match, policy, gate),
            (gate, {"AUTO": approve, "TRIAGE": triage}),
            (approve, archive),
            (triage, ask_review, apply_review, archive),
        ],
    )

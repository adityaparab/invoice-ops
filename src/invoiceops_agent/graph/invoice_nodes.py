"""Invoice workflow node adapters; business work is delegated to injected services."""

import logging
from typing import Protocol

from langgraph.types import interrupt
from pydantic import JsonValue

from invoiceops_agent.graph.state import InvoiceGraphState, InvoiceNodeName, ReviewDecision
from invoiceops_agent.obs.tracing import traced_call
from invoiceops_agent.schemas.exceptions import TaxonomyResult
from invoiceops_agent.schemas.extraction import (
    ExtractionResult,
    ExtractionSuccess,
)
from invoiceops_agent.schemas.gate import GateOutcome
from invoiceops_agent.schemas.matching import ERPSnapshot, MatchResult
from invoiceops_agent.schemas.policy import PolicyResult
from invoiceops_agent.schemas.similarity import SimilarityResult
from invoiceops_agent.schemas.validation import ValidationResult

logger = logging.getLogger(__name__)


class InvoiceServices(Protocol):
    async def extract(self, state: InvoiceGraphState) -> ExtractionResult: ...

    async def validate(self, state: InvoiceGraphState) -> ValidationResult: ...

    async def match(self, state: InvoiceGraphState) -> tuple[ERPSnapshot | None, MatchResult]: ...

    async def policy(
        self, state: InvoiceGraphState
    ) -> tuple[SimilarityResult | None, TaxonomyResult, PolicyResult]: ...

    async def gate(self, state: InvoiceGraphState) -> GateOutcome: ...

    async def triage(self, state: InvoiceGraphState) -> dict[str, JsonValue]: ...

    async def auto_approve(self, state: InvoiceGraphState) -> None: ...

    async def review(self, state: InvoiceGraphState, decision: ReviewDecision) -> None: ...

    async def archive(self, state: InvoiceGraphState) -> None: ...

    async def reject(self, state: InvoiceGraphState) -> None: ...


def _completed(state: InvoiceGraphState, node: InvoiceNodeName) -> list[InvoiceNodeName]:
    logger.info(
        "invoice_graph_node_completed run_id=%s trace_id=%s node=%s",
        state.run_id,
        state.trace_id,
        node,
    )
    return [*state.completed_nodes, node]


class InvoiceNodes:
    def __init__(self, services: InvoiceServices) -> None:
        self.services = services

    async def ingest(self, state: InvoiceGraphState) -> dict[str, object]:
        return {
            "status": "running",
            "route": "REJECT" if state.duplicate else None,
            "completed_nodes": _completed(state, "Ingest"),
        }

    async def extract(self, state: InvoiceGraphState) -> dict[str, object]:
        outcome = await traced_call("tool", "extract", state, lambda: self.services.extract(state))
        extraction = (
            outcome.extraction.model_dump(mode="json")
            if isinstance(outcome, ExtractionSuccess)
            else None
        )
        return {
            "extraction_outcome": outcome.model_dump(mode="json"),
            "extraction": extraction,
            "completed_nodes": _completed(state, "Extract"),
        }

    async def validate(self, state: InvoiceGraphState) -> dict[str, object]:
        result = await traced_call("tool", "validate", state, lambda: self.services.validate(state))
        return {
            "validation": result.model_dump(mode="json"),
            "completed_nodes": _completed(state, "Validate"),
        }

    async def match_three_way(self, state: InvoiceGraphState) -> dict[str, object]:
        snapshot, result = await traced_call(
            "tool", "match", state, lambda: self.services.match(state)
        )
        return {
            "snapshot": snapshot.model_dump(mode="json") if snapshot is not None else None,
            "match": result.model_dump(mode="json"),
            "completed_nodes": _completed(state, "Match3Way"),
        }

    async def policy(self, state: InvoiceGraphState) -> dict[str, object]:
        similarity, taxonomy, decision = await traced_call(
            "tool", "policy", state, lambda: self.services.policy(state)
        )
        return {
            "similarity": similarity.model_dump(mode="json") if similarity is not None else None,
            "taxonomy": taxonomy.model_dump(mode="json"),
            "policy": decision.model_dump(mode="json"),
            "completed_nodes": _completed(state, "Policy"),
        }

    async def gate(self, state: InvoiceGraphState) -> dict[str, object]:
        result = await traced_call("tool", "gate", state, lambda: self.services.gate(state))
        return {
            "gate": result.model_dump(mode="json"),
            "route": result.route,
            "completed_nodes": _completed(state, "Gate"),
        }

    async def auto_approve(self, state: InvoiceGraphState) -> dict[str, object]:
        await traced_call("tool", "auto_approve", state, lambda: self.services.auto_approve(state))
        return {"completed_nodes": _completed(state, "AutoApprove")}

    async def exception_triage(self, state: InvoiceGraphState) -> dict[str, object]:
        result = await traced_call("tool", "triage", state, lambda: self.services.triage(state))
        return {
            "status": "awaiting_review",
            "route": "REVIEW",
            "triage": result,
            "completed_nodes": _completed(state, "ExceptionTriage"),
        }

    async def human_review(self, state: InvoiceGraphState) -> dict[str, object]:
        value = interrupt(
            {
                "run_id": str(state.run_id),
                "invoice_id": str(state.invoice_id),
                "triage": state.triage,
            }
        )
        decision = ReviewDecision.model_validate(value)
        return await self.apply_review(state, decision)

    async def apply_review(
        self, state: InvoiceGraphState, decision: ReviewDecision
    ) -> dict[str, object]:
        await traced_call("tool", "review", state, lambda: self.services.review(state, decision))
        return {
            "review": decision.model_dump(mode="json"),
            "status": "running",
            "completed_nodes": _completed(state, "HumanReview"),
        }

    async def archive(self, state: InvoiceGraphState) -> dict[str, object]:
        await traced_call("tool", "archive", state, lambda: self.services.archive(state))
        return {
            "status": "completed",
            "completed_nodes": _completed(state, "Archive"),
        }

    async def reject(self, state: InvoiceGraphState) -> dict[str, object]:
        await traced_call("tool", "reject", state, lambda: self.services.reject(state))
        return {
            "status": "rejected",
            "completed_nodes": _completed(state, "Reject"),
        }

"""Concrete invoice workflow adapters with ledger-backed replay of committed nodes."""

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

import psycopg
from psycopg.rows import DictRow
from pydantic import BaseModel, JsonValue, TypeAdapter

from invoiceops_agent.agents.extraction import ExtractionAgent
from invoiceops_agent.agents.near_duplicate import NearDuplicateAgent
from invoiceops_agent.graph.nodes.exception_taxonomy import ExceptionTaxonomyNode
from invoiceops_agent.graph.nodes.match3way import Match3WayNode
from invoiceops_agent.graph.nodes.policy import PolicyNode
from invoiceops_agent.graph.nodes.validate import ValidateNode
from invoiceops_agent.graph.state import InvoiceGraphState, ReviewDecision
from invoiceops_agent.ledger.audit import AuditSink, AuditWriter
from invoiceops_agent.ledger.schemas import AppendEvent, VersionOverrides
from invoiceops_agent.schemas.common import model_digest
from invoiceops_agent.schemas.documents import DocumentReference
from invoiceops_agent.schemas.exceptions import TaxonomyRequest, TaxonomyResult
from invoiceops_agent.schemas.extraction import (
    ExtractionRequest,
    ExtractionResult,
    InvoiceExtraction,
)
from invoiceops_agent.schemas.gate import (
    CompositeGateConfig,
    CompositeGateResult,
    GateOutcome,
)
from invoiceops_agent.schemas.matching import ERPSnapshot, MatchRequest, MatchResult
from invoiceops_agent.schemas.policy import PolicyRequest, PolicyResult
from invoiceops_agent.schemas.similarity import SimilarityRequest, SimilarityResult
from invoiceops_agent.schemas.validation import ValidationRequest, ValidationResult
from invoiceops_agent.tools.erp_repository import ERPRepository
from invoiceops_agent.tools.gate import evaluate_composite_gate
from invoiceops_agent.tools.policy import po_age_exceeded

logger = logging.getLogger(__name__)
type ConnectionFactory = Callable[[], AbstractAsyncContextManager[psycopg.AsyncConnection[DictRow]]]
_extraction_adapter: TypeAdapter[ExtractionResult] = TypeAdapter(ExtractionResult)
_gate_adapter: TypeAdapter[GateOutcome] = TypeAdapter(GateOutcome)


class ReplayEvidenceError(Exception):
    """Previously committed evidence cannot be reconciled with current inputs."""


class EventCache:
    def __init__(self, connection: ConnectionFactory) -> None:
        self._connection = connection

    async def read(
        self, state: InvoiceGraphState, *event_types: str
    ) -> dict[str, JsonValue] | None:
        async with self._connection() as connection:
            cursor = await connection.execute(
                "SELECT payload FROM public.ledger WHERE run_id = %s AND invoice_id = %s "
                "AND event_type = ANY(%s::text[]) ORDER BY sequence DESC LIMIT 1",
                (state.run_id, state.invoice_id, list(event_types)),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        payload = row["payload"]
        if not isinstance(payload, dict):
            raise ReplayEvidenceError("Committed workflow evidence is not an object")
        return payload


class WorkflowTransitions:
    def __init__(self, connection: ConnectionFactory, writer: AuditWriter) -> None:
        self._connection = connection
        self._writer = writer

    async def append_once(
        self,
        state: InvoiceGraphState,
        *,
        event_type: str,
        node: str,
        actor_type: str,
        actor_id: str,
        payload: dict[str, JsonValue],
        invoice_status: str | None = None,
        model_version: str = "not-applicable@v1",
        prompt_version: str = "not-applicable@v1",
        policy_version: str = "invoice-workflow@v1",
    ) -> None:
        async with self._connection() as connection, connection.transaction():
            cursor = await connection.execute(
                "SELECT payload FROM public.ledger WHERE run_id = %s AND invoice_id = %s "
                "AND event_type = %s LIMIT 1",
                (state.run_id, state.invoice_id, event_type),
            )
            committed = await cursor.fetchone()
            if committed is not None:
                if committed["payload"] != payload:
                    raise ReplayEvidenceError(
                        "Committed transition differs from requested transition"
                    )
                return
            if invoice_status is not None:
                updated = await connection.execute(
                    "UPDATE public.invoices SET status = %s WHERE id = %s RETURNING id",
                    (invoice_status, state.invoice_id),
                )
                if await updated.fetchone() is None:
                    raise ReplayEvidenceError("Invoice to transition is missing")
            await self._writer.append(
                connection,
                AppendEvent(
                    run_id=state.run_id,
                    invoice_id=state.invoice_id,
                    event_type=event_type,
                    node=node,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    versions=VersionOverrides(
                        model_version=model_version,
                        prompt_version=prompt_version,
                        policy_version=policy_version,
                    ),
                    payload=payload,
                ),
                trace_id=state.trace_id,
            )


class LiveInvoiceServices:
    def __init__(
        self,
        *,
        connection: ConnectionFactory,
        extraction: ExtractionAgent,
        validation: ValidateNode,
        matching: Match3WayNode,
        similarity: NearDuplicateAgent,
        taxonomy: ExceptionTaxonomyNode,
        policy: PolicyNode,
        audit: AuditSink,
        audit_writer: AuditWriter,
        gate_config: CompositeGateConfig | None = None,
    ) -> None:
        self._connection = connection
        self._extraction = extraction
        self._validation = validation
        self._matching = matching
        self._similarity = similarity
        self._taxonomy = taxonomy
        self._policy = policy
        self._audit = audit
        self._events = EventCache(connection)
        self._transitions = WorkflowTransitions(connection, audit_writer)
        self._policy_config = policy.config
        self._gate_config = gate_config if gate_config is not None else CompositeGateConfig()

    async def extract(self, state: InvoiceGraphState) -> ExtractionResult:
        cached = await self._events.read(state, "extraction.completed", "extraction.escalated")
        if cached is not None:
            if cached.get("content_hash") != state.content_hash:
                raise ReplayEvidenceError("Extraction source changed after committed evidence")
            result: ExtractionResult = _extraction_adapter.validate_python(cached["result"])
            if (
                result.run_id != state.run_id
                or result.invoice_id != state.invoice_id
                or result.trace_id != state.trace_id
            ):
                raise ReplayEvidenceError("Extraction identity differs from the workflow")
            return result
        document = DocumentReference(
            raw_ref=state.raw_ref,
            content_hash=state.content_hash,
            content_type=state.content_type,
        )
        return await self._extraction.extract(
            ExtractionRequest(
                **document.model_dump(),
                run_id=state.run_id,
                invoice_id=state.invoice_id,
                trace_id=state.trace_id,
            )
        )

    async def validate(self, state: InvoiceGraphState) -> ValidationResult:
        extraction = self._required_extraction(state)
        cached = await self._events.read(state, "validation.completed")
        if cached is not None:
            result = ValidationResult.model_validate(cached)
            self._require_digest(result.input_sha256, extraction, "Validation")
            return result
        return await self._validation.run(
            ValidationRequest(
                run_id=state.run_id,
                invoice_id=state.invoice_id,
                trace_id=state.trace_id,
                extraction=extraction,
            )
        )

    async def match(self, state: InvoiceGraphState) -> tuple[ERPSnapshot | None, MatchResult]:
        extraction = self._required_extraction(state)
        async with self._connection() as connection:
            po_number = extraction.po_number.value
            snapshot = await ERPRepository.snapshot(connection, po_number) if po_number else None
        cached = await self._events.read(state, "matching.completed")
        if cached is not None:
            result = MatchResult.model_validate(cached)
            self._require_digest(result.extraction_sha256, extraction, "Matching extraction")
            if result.snapshot_sha256 != (model_digest(snapshot) if snapshot is not None else None):
                raise ReplayEvidenceError("ERP snapshot changed after committed matching evidence")
            return snapshot, result
        result = await self._matching.run(
            MatchRequest(
                run_id=state.run_id,
                invoice_id=state.invoice_id,
                trace_id=state.trace_id,
                extraction=extraction,
                snapshot=snapshot,
            )
        )
        return snapshot, result

    async def policy(
        self, state: InvoiceGraphState
    ) -> tuple[SimilarityResult | None, TaxonomyResult, PolicyResult]:
        extraction = self._required_extraction(state)
        validation = self._required_validation(state)
        match = self._required_match(state)
        snapshot = (
            ERPSnapshot.model_validate(state.snapshot) if state.snapshot is not None else None
        )
        cached_similarity = await self._events.read(state, "similarity.completed")
        similarity = (
            SimilarityResult.model_validate(cached_similarity)
            if cached_similarity is not None
            else await self._similarity.detect(
                SimilarityRequest(
                    run_id=state.run_id,
                    invoice_id=state.invoice_id,
                    trace_id=state.trace_id,
                    extraction=extraction,
                )
            )
        )
        self._require_digest(similarity.extraction_sha256, extraction, "Similarity")
        taxonomy_request = TaxonomyRequest(
            run_id=state.run_id,
            invoice_id=state.invoice_id,
            trace_id=state.trace_id,
            extraction=extraction,
            validation=validation,
            match=match,
            vendor_bank_iban=snapshot.vendor.bank_account_iban if snapshot else None,
            near_duplicate=similarity.status == "NEAR_DUPLICATE",
            stale_po=po_age_exceeded(snapshot, state.as_of, self._policy_config.max_po_age_days),
        )
        cached_taxonomy = await self._events.read(state, "classification.completed")
        taxonomy = (
            TaxonomyResult.model_validate(cached_taxonomy)
            if cached_taxonomy is not None
            else await self._taxonomy.run(taxonomy_request)
        )
        self._require_digest(taxonomy.input_sha256, taxonomy_request, "Taxonomy")
        policy_request = PolicyRequest(
            run_id=state.run_id,
            invoice_id=state.invoice_id,
            trace_id=state.trace_id,
            as_of=state.as_of,
            extraction=extraction,
            validation=validation,
            match=match,
            taxonomy=taxonomy,
            snapshot=snapshot,
        )
        cached_policy = await self._events.read(state, "policy.completed")
        decision = (
            PolicyResult.model_validate(cached_policy)
            if cached_policy is not None
            else await self._policy.run(policy_request)
        )
        self._require_digest(decision.input_sha256, policy_request, "Policy")
        return similarity, taxonomy, decision

    async def gate(self, state: InvoiceGraphState) -> GateOutcome:
        request = self._gate_request(state)
        policy = PolicyResult.model_validate(state.policy)
        self._require_digest(policy.input_sha256, request, "Gate policy inputs")
        cached = await self._events.read(state, "gate.completed")
        if cached is not None:
            result = _gate_adapter.validate_python(cached)
            self._require_digest(
                result.extraction_sha256, self._required_extraction(state), "Gate extraction"
            )
            self._require_digest(result.policy_sha256, policy, "Gate policy")
            if isinstance(result, CompositeGateResult):
                self._require_digest(
                    result.match_sha256, self._required_match(state), "Gate matching"
                )
            return result
        result = evaluate_composite_gate(request, policy, self._gate_config)
        await self._audit.append(
            AppendEvent(
                run_id=state.run_id,
                invoice_id=state.invoice_id,
                event_type="gate.completed",
                node="Gate",
                actor_type="POLICY",
                actor_id="invoiceops-provisional-gate",
                versions=VersionOverrides(
                    model_version="not-applicable@v1",
                    prompt_version="not-applicable@v1",
                    policy_version=self._gate_config.version,
                ),
                payload=result.model_dump(mode="json"),
            ),
            trace_id=state.trace_id,
        )
        return result

    async def triage(self, state: InvoiceGraphState) -> dict[str, JsonValue]:
        cached = await self._events.read(state, "triage.prepared")
        if cached is not None:
            return cached
        taxonomy = TaxonomyResult.model_validate(state.taxonomy) if state.taxonomy else None
        payload: dict[str, JsonValue] = {
            "recommendation": "REVIEW",
            "codes": list(taxonomy.codes) if taxonomy else [],
            "extraction_escalated": state.extraction is None,
        }
        await self._audit.append(
            AppendEvent(
                run_id=state.run_id,
                invoice_id=state.invoice_id,
                event_type="triage.prepared",
                node="ExceptionTriage",
                actor_type="SYSTEM",
                actor_id="invoiceops-triage-placeholder",
                versions=VersionOverrides(
                    model_version="not-applicable@v1",
                    prompt_version="not-applicable@v1",
                    policy_version="triage-placeholder@v1",
                ),
                payload=payload,
            ),
            trace_id=state.trace_id,
        )
        return payload

    async def auto_approve(self, state: InvoiceGraphState) -> None:
        await self._transitions.append_once(
            state,
            event_type="approval.auto_granted",
            node="AutoApprove",
            actor_type="POLICY",
            actor_id="invoiceops-auto-approval",
            payload={"policy_sha256": PolicyResult.model_validate(state.policy).input_sha256},
            invoice_status="APPROVED",
        )

    async def review(self, state: InvoiceGraphState, decision: ReviewDecision) -> None:
        status = {"APPROVE": "APPROVED", "RETURN": "RETURNED", "ESCALATE": "NEEDS_REVIEW"}[
            decision.action
        ]
        await self._transitions.append_once(
            state,
            event_type="review.recorded",
            node="HumanReview",
            actor_type="HUMAN",
            actor_id=decision.actor_id,
            payload=decision.model_dump(mode="json"),
            invoice_status=status,
        )

    async def archive(self, state: InvoiceGraphState) -> None:
        await self._transitions.append_once(
            state,
            event_type="workflow.archived",
            node="Archive",
            actor_type="SYSTEM",
            actor_id="invoiceops-workflow",
            payload={"route": state.route or "REVIEW"},
        )

    async def reject(self, state: InvoiceGraphState) -> None:
        await self._transitions.append_once(
            state,
            event_type="workflow.rejected",
            node="Reject",
            actor_type="SYSTEM",
            actor_id="invoiceops-workflow",
            payload={"reason": "EXACT_DUPLICATE"},
        )

    @staticmethod
    def _required_extraction(state: InvoiceGraphState) -> InvoiceExtraction:
        if state.extraction is None:
            raise ReplayEvidenceError("Extraction is unavailable for workflow node")
        return InvoiceExtraction.model_validate(state.extraction)

    @staticmethod
    def _required_validation(state: InvoiceGraphState) -> ValidationResult:
        if state.validation is None:
            raise ReplayEvidenceError("Validation is unavailable for workflow node")
        return ValidationResult.model_validate(state.validation)

    @staticmethod
    def _required_match(state: InvoiceGraphState) -> MatchResult:
        if state.match is None:
            raise ReplayEvidenceError("Matching is unavailable for workflow node")
        return MatchResult.model_validate(state.match)

    @staticmethod
    def _gate_request(state: InvoiceGraphState) -> PolicyRequest:
        if state.taxonomy is None:
            raise ReplayEvidenceError("Taxonomy is unavailable for confidence gating")
        return PolicyRequest(
            run_id=state.run_id,
            invoice_id=state.invoice_id,
            trace_id=state.trace_id,
            as_of=state.as_of,
            extraction=LiveInvoiceServices._required_extraction(state),
            validation=LiveInvoiceServices._required_validation(state),
            match=LiveInvoiceServices._required_match(state),
            taxonomy=TaxonomyResult.model_validate(state.taxonomy),
            snapshot=ERPSnapshot.model_validate(state.snapshot)
            if state.snapshot is not None
            else None,
        )

    @staticmethod
    def _require_digest(expected: str, value: BaseModel, label: str) -> None:
        if expected != model_digest(value):
            raise ReplayEvidenceError(f"{label} input changed after committed evidence")

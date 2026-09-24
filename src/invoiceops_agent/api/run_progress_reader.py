"""Read-only run progress projected from committed workflow audit events."""

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from time import perf_counter
from typing import Protocol, cast
from uuid import UUID

import psycopg
from pydantic import JsonValue, ValidationError

from invoiceops_agent.api.read_store import ReadStoreUnavailable, connect_read_store
from invoiceops_agent.api.schemas.run_progress import NodeProgress, RunProgress
from invoiceops_agent.api.settings import ApiSettings
from invoiceops_agent.graph.state import InvoiceNodeName

logger = logging.getLogger(__name__)

NODE_EVENTS: tuple[tuple[InvoiceNodeName, tuple[str, ...]], ...] = (
    ("Ingest", ("ingest.accepted",)),
    ("Extract", ("extraction.completed", "extraction.escalated")),
    ("Validate", ("validation.completed",)),
    ("Match3Way", ("matching.completed",)),
    ("Policy", ("policy.completed",)),
    ("Gate", ("gate.completed",)),
    ("AutoApprove", ("approval.auto_granted",)),
    ("ExceptionTriage", ("triage.prepared",)),
    ("HumanReview", ("review.recorded",)),
    ("Archive", ("workflow.archived",)),
    ("Reject", ("workflow.rejected",)),
)
_EVENT_TO_NODE = {event: node for node, events in NODE_EVENTS for event in events}
_EVENTS = list(_EVENT_TO_NODE)
_STATE_KEYS: dict[InvoiceNodeName, tuple[str, ...]] = {
    "Ingest": ("source", "content_type", "size_bytes"),
    "Extract": ("status", "reason"),
    "Validate": ("status", "issues"),
    "Match3Way": ("status", "identity_checks", "numeric_checks"),
    "Policy": ("status", "reasons"),
    "Gate": ("route", "reason", "score", "minimum_observed_confidence"),
    "AutoApprove": ("action", "reason"),
    "ExceptionTriage": ("recommendation", "codes", "extraction_escalated"),
    "HumanReview": ("action", "reason_code", "actor_id"),
    "Archive": ("status",),
    "Reject": ("reason",),
}


class RunProgressError(Exception):
    """Sanitized read failure."""


class RunNotFound(RunProgressError):
    """The requested run does not exist."""


class RunProgressUnavailable(RunProgressError):
    """The operational run or audit store is unavailable."""


class RunProgressReader(Protocol):
    async def read(self, run_id: UUID) -> RunProgress: ...


def _state(node: InvoiceNodeName, payload: object) -> dict[str, JsonValue]:
    if not isinstance(payload, dict):
        raise RunProgressUnavailable("Run progress contains invalid audit data")
    source = payload.get("result") if node == "Extract" else payload
    if not isinstance(source, dict):
        raise RunProgressUnavailable("Run progress contains invalid audit data")
    result = {key: cast(JsonValue, source[key]) for key in _STATE_KEYS[node] if key in source}
    if node == "Extract":
        extraction = source.get("extraction")
        if isinstance(extraction, dict):
            fields = ("vendor_name", "invoice_number", "po_number", "currency", "total_amount")
            result["fields"] = {
                name: cast(JsonValue, extraction[name]) for name in fields if name in extraction
            }
    return result


def _active_node(
    status: str, observed: Mapping[InvoiceNodeName, NodeProgress]
) -> InvoiceNodeName | None:
    if status in {"QUEUED", "COMPLETED", "FAILED", "CANCELLED"}:
        return None
    if status == "PAUSED":
        return "HumanReview"
    for name, _ in reversed(NODE_EVENTS):
        if name not in observed:
            continue
        if name in {"Archive", "Reject"}:
            return None
        if name == "Ingest":
            return "Extract"
        if name == "Extract":
            return (
                "ExceptionTriage"
                if observed[name].event_type == "extraction.escalated"
                else "Validate"
            )
        if name == "Validate":
            return "Match3Way"
        if name == "Match3Way":
            return "Policy"
        if name == "Policy":
            return "Gate"
        if name == "Gate":
            return (
                "AutoApprove"
                if observed[name].state.get("route") == "AUTO_APPROVE"
                else "ExceptionTriage"
            )
        if name in {"AutoApprove", "HumanReview"}:
            return "Archive"
        if name == "ExceptionTriage":
            return "HumanReview"
    return "Ingest"


class PostgresRunProgressReader:
    def __init__(
        self, settings: ApiSettings, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._settings = settings
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)

    async def read(self, run_id: UUID) -> RunProgress:
        started = perf_counter()
        try:
            async with await connect_read_store(self._settings) as connection:
                row = await (
                    await connection.execute(
                        "SELECT id, invoice_id, status, graph_version, started_at, completed_at "
                        "FROM public.runs WHERE id = %s",
                        (run_id,),
                    )
                ).fetchone()
                if row is None:
                    raise RunNotFound("Run does not exist")
                events = await (
                    await connection.execute(
                        "SELECT DISTINCT ON (node) event_type, payload, created_at "
                        "FROM public.ledger WHERE run_id = %s AND event_type = ANY(%s::text[]) "
                        "ORDER BY node, sequence DESC",
                        (run_id, _EVENTS),
                    )
                ).fetchall()
            observed: dict[InvoiceNodeName, NodeProgress] = {}
            for event in events:
                event_type = str(event["event_type"])
                node = _EVENT_TO_NODE[event_type]
                if node in observed:
                    continue
                observed[node] = NodeProgress.model_validate(
                    {
                        "name": node,
                        "observed_at": event["created_at"],
                        "event_type": event_type,
                        "state": _state(node, event["payload"]),
                    }
                )
            progress = RunProgress.model_validate(
                {
                    "run_id": row["id"],
                    "invoice_id": row["invoice_id"],
                    "status": row["status"],
                    "graph_version": row["graph_version"],
                    "active_node": _active_node(str(row["status"]), observed),
                    "nodes": [
                        observed.get(name, NodeProgress(name=name)) for name, _ in NODE_EVENTS
                    ],
                    "started_at": row["started_at"],
                    "completed_at": row["completed_at"],
                    "read_at": self._clock(),
                }
            )
        except RunNotFound:
            raise
        except (ReadStoreUnavailable, psycopg.Error, ValidationError, KeyError) as error:
            logger.error(
                "run_progress_read_failed run_id=%s error_type=%s", run_id, type(error).__name__
            )
            raise RunProgressUnavailable("Run progress read failed") from error
        logger.info(
            "run_progress_read run_id=%s duration_ms=%.3f",
            run_id,
            (perf_counter() - started) * 1000,
        )
        return progress

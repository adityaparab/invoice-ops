"""Atomic append-only proposal and countersign writes for human decisions."""

import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from invoiceops_agent.api.read_auth import ReadRole
from invoiceops_agent.api.schemas.decision import DecisionRequest, DecisionResponse
from invoiceops_agent.api.settings import ApiSettings
from invoiceops_agent.ledger.errors import LedgerError
from invoiceops_agent.ledger.schemas import AppendEvent, VersionOverrides
from invoiceops_agent.ledger.settings import LedgerSettings
from invoiceops_agent.ledger.writer import LedgerWriter

logger = logging.getLogger(__name__)
_ACTORS: dict[ReadRole, str] = {
    "ANALYST": "maria-ap-analyst",
    "MANAGER": "dan-procurement-manager",
    "AUDITOR": "priya-auditor",
}
_POLICY_VERSION = "four-eyes@v1"


class DecisionError(Exception):
    """Sanitized decision failure at the API boundary."""


class DecisionNotFound(DecisionError):
    """The requested exception is absent."""


class DecisionConflict(DecisionError):
    """The decision conflicts with current state or its replay key."""


class DecisionUnavailable(DecisionError):
    """The operational decision store is unavailable."""


class DecisionWriter(Protocol):
    async def submit(
        self,
        *,
        exception_id: UUID,
        request: DecisionRequest,
        role: ReadRole,
        idempotency_key: str,
        trace_id: str,
    ) -> DecisionResponse: ...


class DecisionService:
    def __init__(
        self,
        settings: ApiSettings,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._new_id = new_id

    async def submit(
        self,
        *,
        exception_id: UUID,
        request: DecisionRequest,
        role: ReadRole,
        idempotency_key: str,
        trace_id: str,
    ) -> DecisionResponse:
        if role == "AUDITOR":
            raise DecisionConflict("Auditors cannot make exception decisions")
        if self._settings.postgres_dsn is None:
            raise DecisionUnavailable("Decision persistence is not configured")
        actor_id = _ACTORS[role]
        try:
            async with (
                await psycopg.AsyncConnection.connect(
                    self._settings.postgres_dsn.get_secret_value(),
                    autocommit=True,
                    row_factory=dict_row,
                    connect_timeout=5,
                    options="-c statement_timeout=10000 -c lock_timeout=10000",
                ) as connection,
                connection.transaction(),
            ):
                lock_key = int.from_bytes(
                    hashlib.sha256(f"decision:{idempotency_key}".encode()).digest()[:8],
                    signed=True,
                )
                await connection.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
                existing = await (
                    await connection.execute(
                        "SELECT id, exception_id, run_id, invoice_id, action, rationale, "
                        "reason_code, actor_id, supersedes_id FROM public.decisions "
                        "WHERE idempotency_key = %s",
                        (idempotency_key,),
                    )
                ).fetchone()
                if existing is not None:
                    if (
                        existing["exception_id"] != exception_id
                        or existing["action"] != request.action
                        or existing["rationale"] != request.rationale
                        or existing["reason_code"] != request.reason_code
                        or existing["actor_id"] != actor_id
                        or existing["supersedes_id"] != request.proposal_id
                    ):
                        raise DecisionConflict("Idempotency key belongs to a different decision")
                    return self._response(existing)
                row = await (
                    await connection.execute(
                        "SELECT e.id AS exception_id, e.run_id, e.invoice_id, e.status, "
                        "r.status AS run_status, r.graph_version, r.trace_id "
                        "FROM public.exceptions e JOIN public.runs r ON r.id = e.run_id "
                        "WHERE e.id = %s FOR UPDATE OF r, e",
                        (exception_id,),
                    )
                ).fetchone()
                if row is None:
                    raise DecisionNotFound("Exception does not exist")
                if row["run_status"] != "PAUSED" or row["status"] not in {"OPEN", "IN_REVIEW"}:
                    raise DecisionConflict("Exception is not awaiting a human decision")
                decisions = await (
                    await connection.execute(
                        "SELECT id, action, actor_id, supersedes_id FROM public.decisions "
                        "WHERE exception_id = %s ORDER BY created_at, id",
                        (exception_id,),
                    )
                ).fetchall()
                if role == "ANALYST":
                    if request.proposal_id is not None:
                        raise DecisionConflict("Analyst proposals cannot countersign a decision")
                    if decisions:
                        raise DecisionConflict("Exception already has a decision proposal")
                    if row["status"] != "OPEN":
                        raise DecisionConflict("Exception is not open for an analyst proposal")
                    stage = "PENDING_SIGNOFF"
                else:
                    if request.proposal_id is None or len(decisions) != 1:
                        raise DecisionConflict("Manager signoff requires one analyst proposal")
                    proposal = decisions[0]
                    if (
                        proposal["id"] != request.proposal_id
                        or proposal["actor_id"] != _ACTORS["ANALYST"]
                        or proposal["supersedes_id"] is not None
                    ):
                        raise DecisionConflict(
                            "Manager signoff does not match the analyst proposal"
                        )
                    if request.action == "APPROVE" and proposal["action"] != "APPROVE":
                        raise DecisionConflict("Approval requires an analyst approval proposal")
                    if row["status"] != "IN_REVIEW":
                        raise DecisionConflict("Exception proposal is no longer in review")
                    stage = "QUEUED_FOR_RESUME"
                decision_id = self._new_id()
                created_at = self._clock()
                await connection.execute(
                    "INSERT INTO public.decisions "
                    "(id, run_id, invoice_id, exception_id, idempotency_key, action, "
                    "rationale, reason_code, actor_type, actor_id, graph_version, "
                    "model_version, prompt_version, policy_version, supersedes_id, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'HUMAN', %s, %s, "
                    "'not-applicable@v1', 'not-applicable@v1', %s, %s, %s)",
                    (
                        decision_id,
                        row["run_id"],
                        row["invoice_id"],
                        exception_id,
                        idempotency_key,
                        request.action,
                        request.rationale,
                        request.reason_code,
                        actor_id,
                        row["graph_version"],
                        _POLICY_VERSION,
                        request.proposal_id,
                        created_at,
                    ),
                )
                if role == "ANALYST":
                    await connection.execute(
                        "UPDATE public.exceptions SET status = 'IN_REVIEW' WHERE id = %s",
                        (exception_id,),
                    )
                writer = LedgerWriter(
                    LedgerSettings(
                        graph_version=row["graph_version"],
                        model_version="not-applicable@v1",
                        prompt_version="not-applicable@v1",
                        policy_version=_POLICY_VERSION,
                        _env_file=None,
                    ),
                    clock=self._clock,
                )
                await writer.append(
                    connection,
                    AppendEvent(
                        run_id=row["run_id"],
                        invoice_id=row["invoice_id"],
                        event_type="decision.proposed"
                        if role == "ANALYST"
                        else "decision.accepted",
                        node="HumanDecision",
                        actor_type="HUMAN",
                        actor_id=actor_id,
                        versions=VersionOverrides(policy_version=_POLICY_VERSION),
                        payload={
                            "decision_id": str(decision_id),
                            "exception_id": str(exception_id),
                            "action": request.action,
                            "rationale": request.rationale,
                            "reason_code": request.reason_code,
                            "proposal_id": str(request.proposal_id)
                            if request.proposal_id is not None
                            else None,
                        },
                    ),
                    trace_id=row["trace_id"],
                )
                return DecisionResponse(
                    decision_id=decision_id,
                    exception_id=exception_id,
                    run_id=row["run_id"],
                    invoice_id=row["invoice_id"],
                    action=request.action,
                    actor_id=actor_id,
                    stage=stage,
                    proposal_id=request.proposal_id,
                )
        except DecisionError:
            raise
        except (psycopg.Error, LedgerError) as error:
            logger.error(
                "decision_write_failed trace_id=%s error_type=%s",
                trace_id,
                type(error).__name__,
            )
            raise DecisionUnavailable("Decision persistence failed") from error

    @staticmethod
    def _response(row: dict[str, object]) -> DecisionResponse:
        return DecisionResponse.model_validate(
            {
                "decision_id": row["id"],
                "exception_id": row["exception_id"],
                "run_id": row["run_id"],
                "invoice_id": row["invoice_id"],
                "action": row["action"],
                "actor_id": row["actor_id"],
                "stage": "PENDING_SIGNOFF"
                if row["actor_id"] == _ACTORS["ANALYST"]
                else "QUEUED_FOR_RESUME",
                "proposal_id": row["supersedes_id"],
            }
        )

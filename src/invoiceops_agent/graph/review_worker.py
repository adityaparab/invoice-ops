"""Resume a signed human decision through the existing durable invoice graph."""

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from invoiceops_agent.graph.runtime import InvoiceRuntimeSettings, invoice_runtime
from invoiceops_agent.graph.state import InvoiceGraphState, ReviewDecision

logger = logging.getLogger(__name__)
type ResumeDecision = Callable[[UUID, ReviewDecision], Awaitable[InvoiceGraphState]]


class ReviewWorkerError(Exception):
    """An accepted decision cannot be settled safely."""


class DecisionResumeWorker:
    def __init__(
        self,
        settings: InvoiceRuntimeSettings,
        *,
        resume: ResumeDecision | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._resume = resume
        self._clock = clock

    async def process(self, decision_id: UUID) -> dict[str, str]:
        dsn = self._settings.postgres_dsn.get_secret_value()
        try:
            async with await psycopg.AsyncConnection.connect(
                dsn,
                autocommit=True,
                row_factory=dict_row,
                connect_timeout=5,
                options="-c statement_timeout=10000 -c lock_timeout=10000",
            ) as connection:
                row = await (
                    await connection.execute(
                        "SELECT d.id, d.run_id, d.invoice_id, d.exception_id, d.action, "
                        "d.actor_id, d.rationale, d.reason_code, d.supersedes_id, "
                        "r.status AS run_status, e.status AS exception_status, "
                        "p.actor_id AS proposal_actor, p.action AS proposal_action "
                        "FROM public.decisions d "
                        "JOIN public.runs r ON r.id = d.run_id "
                        "JOIN public.exceptions e ON e.id = d.exception_id "
                        "LEFT JOIN public.decisions p ON p.id = d.supersedes_id "
                        "WHERE d.id = %s",
                        (decision_id,),
                    )
                ).fetchone()
                if row is None:
                    raise ReviewWorkerError("Accepted decision is missing")
                if (
                    row["actor_id"] != "dan-procurement-manager"
                    or row["supersedes_id"] is None
                    or row["proposal_actor"] != "maria-ap-analyst"
                    or (row["action"] == "APPROVE" and row["proposal_action"] != "APPROVE")
                ):
                    raise ReviewWorkerError("Decision has no valid independent signoff")
                if row["run_status"] not in {"PAUSED", "COMPLETED"}:
                    raise ReviewWorkerError("Decision run is not paused or completed")
                decision = ReviewDecision(
                    action=row["action"],
                    actor_id=row["actor_id"],
                    rationale=row["rationale"],
                    reason_code=row["reason_code"],
                )
                if row["run_status"] == "PAUSED":
                    if self._resume is None:
                        async with invoice_runtime(
                            row["run_id"], settings=self._settings
                        ) as workflow:
                            result = await workflow.resume(decision)
                    else:
                        result = await self._resume(row["run_id"], decision)
                    if (
                        result.status != "completed"
                        or result.run_id != row["run_id"]
                        or result.invoice_id != row["invoice_id"]
                        or result.review != decision.model_dump(mode="json")
                    ):
                        raise ReviewWorkerError("Review graph did not complete this decision")
                async with connection.transaction():
                    locked = await (
                        await connection.execute(
                            "SELECT status FROM public.runs WHERE id = %s FOR UPDATE",
                            (row["run_id"],),
                        )
                    ).fetchone()
                    if locked is None or locked["status"] not in {"PAUSED", "COMPLETED"}:
                        raise ReviewWorkerError("Review run changed before settlement")
                    evidence = await (
                        await connection.execute(
                            "SELECT event_type, payload FROM public.ledger WHERE run_id = %s "
                            "AND event_type IN ('review.recorded', 'workflow.archived')",
                            (row["run_id"],),
                        )
                    ).fetchall()
                    events = {item["event_type"]: item["payload"] for item in evidence}
                    if (
                        events.get("review.recorded") != decision.model_dump(mode="json")
                        or "workflow.archived" not in events
                    ):
                        raise ReviewWorkerError("Committed graph review evidence is incomplete")
                    final_status = "ESCALATED" if decision.action == "ESCALATE" else "RESOLVED"
                    updated = await connection.execute(
                        "UPDATE public.exceptions SET status = %s WHERE id = %s "
                        "AND status IN ('IN_REVIEW', %s)",
                        (final_status, row["exception_id"], final_status),
                    )
                    if updated.rowcount != 1:
                        raise ReviewWorkerError("Exception changed before settlement")
                    if locked["status"] == "PAUSED":
                        await connection.execute(
                            "UPDATE public.runs SET status = 'COMPLETED', completed_at = %s "
                            "WHERE id = %s AND status = 'PAUSED'",
                            (self._clock(), row["run_id"]),
                        )
                logger.info(
                    "review_decision_settled run_id=%s decision_id=%s status=%s",
                    row["run_id"],
                    decision_id,
                    final_status,
                )
                return {
                    "decision_id": str(decision_id),
                    "run_id": str(row["run_id"]),
                    "status": final_status,
                }
        except psycopg.Error as error:
            logger.error(
                "review_decision_failed decision_id=%s error_type=%s",
                decision_id,
                type(error).__name__,
            )
            raise ReviewWorkerError("Review persistence failed") from error

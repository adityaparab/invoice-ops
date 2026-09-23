"""Commit three-way comparison evidence before a future workflow routes it."""

import logging
from time import perf_counter

from invoiceops_agent.ledger.audit import AuditSink
from invoiceops_agent.ledger.errors import LedgerError
from invoiceops_agent.ledger.schemas import AppendEvent, VersionOverrides
from invoiceops_agent.schemas.matching import MatchConfig, MatchRequest, MatchResult
from invoiceops_agent.tools.matching import match_invoice

logger = logging.getLogger(__name__)


class Match3WayNode:
    def __init__(self, audit: AuditSink, config: MatchConfig | None = None) -> None:
        self._audit = audit
        self._config = config if config is not None else MatchConfig()

    async def run(self, request: MatchRequest) -> MatchResult:
        started = perf_counter()
        result = match_invoice(request, self._config)
        try:
            await self._audit.append(
                AppendEvent(
                    run_id=request.run_id,
                    invoice_id=request.invoice_id,
                    event_type="matching.completed",
                    node="Match3Way",
                    actor_type="POLICY",
                    actor_id="invoiceops-three-way-match",
                    versions=VersionOverrides(
                        model_version="not-applicable@v1",
                        prompt_version="not-applicable@v1",
                        policy_version=self._config.version,
                    ),
                    payload=result.model_dump(mode="json"),
                ),
                trace_id=request.trace_id,
            )
        except LedgerError as error:
            logger.error(
                "matching_audit_failed run_id=%s trace_id=%s error_type=%s",
                request.run_id,
                request.trace_id,
                type(error).__name__,
            )
            raise
        logger.info(
            "matching_completed run_id=%s invoice_id=%s trace_id=%s policy_version=%s "
            "status=%s identity_checks=%d numeric_checks=%d duration_ms=%.3f",
            request.run_id,
            request.invoice_id,
            request.trace_id,
            self._config.version,
            result.status,
            len(result.identity_checks),
            len(result.numeric_checks),
            (perf_counter() - started) * 1000,
        )
        return result

"""Audit deterministic policy decisions before workflow routing."""

import logging
from time import perf_counter

from invoiceops_agent.ledger.audit import AuditSink
from invoiceops_agent.ledger.errors import LedgerError
from invoiceops_agent.ledger.schemas import AppendEvent, VersionOverrides
from invoiceops_agent.schemas.policy import PolicyConfig, PolicyRequest, PolicyResult
from invoiceops_agent.tools.policy import evaluate_policy

logger = logging.getLogger(__name__)


class PolicyNode:
    def __init__(self, audit: AuditSink, config: PolicyConfig | None = None) -> None:
        self._audit = audit
        self._config = config if config is not None else PolicyConfig()

    @property
    def config(self) -> PolicyConfig:
        return self._config

    async def run(self, request: PolicyRequest) -> PolicyResult:
        started = perf_counter()
        result = evaluate_policy(request, self._config)
        try:
            await self._audit.append(
                AppendEvent(
                    run_id=request.run_id,
                    invoice_id=request.invoice_id,
                    event_type="policy.completed",
                    node="Policy",
                    actor_type="POLICY",
                    actor_id="invoiceops-policy",
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
                "policy_audit_failed run_id=%s trace_id=%s error_type=%s",
                request.run_id,
                request.trace_id,
                type(error).__name__,
            )
            raise
        logger.info(
            "policy_completed run_id=%s invoice_id=%s trace_id=%s policy_version=%s "
            "status=%s approval_tier=%s findings=%d duration_ms=%.3f",
            request.run_id,
            request.invoice_id,
            request.trace_id,
            self._config.version,
            result.status,
            result.approval_tier,
            len(result.findings),
            (perf_counter() - started) * 1000,
        )
        return result

"""Audit a deterministic decision before exposing it to a future workflow coordinator."""

import logging
from time import perf_counter

from invoiceops_agent.ledger.audit import AuditSink
from invoiceops_agent.ledger.errors import LedgerError
from invoiceops_agent.ledger.schemas import AppendEvent, VersionOverrides
from invoiceops_agent.schemas.validation import (
    ValidationConfig,
    ValidationRequest,
    ValidationResult,
)
from invoiceops_agent.tools.validation import validate_invoice

logger = logging.getLogger(__name__)


class ValidateNode:
    def __init__(self, audit: AuditSink, config: ValidationConfig | None = None) -> None:
        self._audit = audit
        self._config = config if config is not None else ValidationConfig()

    async def run(self, request: ValidationRequest) -> ValidationResult:
        """Each invocation is a new audited attempt; workflow replay belongs to Phase 2."""
        started = perf_counter()
        result = validate_invoice(request.extraction, self._config)
        try:
            await self._audit.append(
                AppendEvent(
                    run_id=request.run_id,
                    invoice_id=request.invoice_id,
                    event_type="validation.completed",
                    node="Validate",
                    actor_type="POLICY",
                    actor_id="invoiceops-validation",
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
                "validation_audit_failed run_id=%s trace_id=%s error_type=%s",
                request.run_id,
                request.trace_id,
                type(error).__name__,
            )
            raise
        logger.info(
            "validation_completed run_id=%s invoice_id=%s trace_id=%s policy_version=%s "
            "status=%s issue_count=%d duration_ms=%.3f",
            request.run_id,
            request.invoice_id,
            request.trace_id,
            self._config.version,
            result.status,
            len(result.issues),
            (perf_counter() - started) * 1000,
        )
        return result

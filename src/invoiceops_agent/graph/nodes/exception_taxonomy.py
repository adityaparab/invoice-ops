"""Persist exception classification evidence before returning its decision."""

import logging
from time import perf_counter

from invoiceops_agent.ledger.audit import AuditSink
from invoiceops_agent.ledger.errors import LedgerError
from invoiceops_agent.ledger.schemas import AppendEvent, VersionOverrides
from invoiceops_agent.schemas.exceptions import TaxonomyConfig, TaxonomyRequest, TaxonomyResult
from invoiceops_agent.tools.exception_taxonomy import classify_exceptions

logger = logging.getLogger(__name__)


class ExceptionTaxonomyNode:
    def __init__(self, audit: AuditSink, config: TaxonomyConfig | None = None) -> None:
        self._audit = audit
        self._config = config if config is not None else TaxonomyConfig()

    async def run(self, request: TaxonomyRequest) -> TaxonomyResult:
        started = perf_counter()
        result = classify_exceptions(request, self._config)
        try:
            await self._audit.append(
                AppendEvent(
                    run_id=request.run_id,
                    invoice_id=request.invoice_id,
                    event_type="classification.completed",
                    node="ExceptionTaxonomy",
                    actor_type="POLICY",
                    actor_id="invoiceops-exception-taxonomy",
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
                "classification_audit_failed run_id=%s trace_id=%s error_type=%s",
                request.run_id,
                request.trace_id,
                type(error).__name__,
            )
            raise
        logger.info(
            "classification_completed run_id=%s invoice_id=%s trace_id=%s "
            "policy_version=%s status=%s findings=%d unresolved=%d duration_ms=%.3f",
            request.run_id,
            request.invoice_id,
            request.trace_id,
            self._config.version,
            result.status,
            len(result.findings),
            len(result.unresolved),
            (perf_counter() - started) * 1000,
        )
        return result

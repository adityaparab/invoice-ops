"""Embed invoice evidence through LiteLLM and audit the pgvector decision."""

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from time import perf_counter
from typing import Protocol

import psycopg
from psycopg.rows import DictRow
from pydantic import ValidationError

from invoiceops_agent.gateway_client.schemas import EmbeddingRequest, EmbeddingValue, GatewayResult
from invoiceops_agent.ledger.audit import AuditWriter
from invoiceops_agent.ledger.schemas import AppendEvent, VersionOverrides
from invoiceops_agent.obs.tracing import traced_call
from invoiceops_agent.schemas.common import model_digest
from invoiceops_agent.schemas.similarity import (
    SUMMARY_VERSION,
    EmbeddingVector,
    SimilarityConfig,
    SimilarityRequest,
    SimilarityResult,
)
from invoiceops_agent.tools.similarity import invoice_summary
from invoiceops_agent.tools.similarity_repository import SimilarityRepository

logger = logging.getLogger(__name__)


class SimilarityGateway(Protocol):
    async def embed(self, request: EmbeddingRequest) -> GatewayResult[EmbeddingValue]: ...


class EmbeddingContractError(Exception):
    """The gateway returned an embedding that cannot support the pgvector contract."""


class NearDuplicateAgent:
    def __init__(
        self,
        gateway: SimilarityGateway,
        connection: Callable[[], AbstractAsyncContextManager[psycopg.AsyncConnection[DictRow]]],
        audit_writer: AuditWriter,
        config: SimilarityConfig | None = None,
    ) -> None:
        self._gateway = gateway
        self._connection = connection
        self._audit_writer = audit_writer
        self._config = config if config is not None else SimilarityConfig()

    async def detect(self, request: SimilarityRequest) -> SimilarityResult:
        started = perf_counter()
        embedded = await traced_call(
            "tool",
            "similarity_embedding",
            request,
            lambda: self._gateway.embed(
                EmbeddingRequest(
                    run_id=request.run_id,
                    trace_id=request.trace_id,
                    prompt_version=SUMMARY_VERSION,
                    scenario="near_duplicate",
                    inputs=(invoice_summary(request.extraction),),
                )
            ),
        )
        try:
            if len(embedded.value.vectors) != 1:
                raise EmbeddingContractError("Expected one invoice embedding")
            vector = EmbeddingVector(values=embedded.value.vectors[0])
        except ValidationError:
            logger.error(
                "similarity_embedding_invalid run_id=%s trace_id=%s model_version=%s",
                request.run_id,
                request.trace_id,
                embedded.provenance.model_version,
            )
            raise EmbeddingContractError(
                "Expected a finite nonzero 384-dimension embedding"
            ) from None
        model_version = embedded.provenance.model_version
        async with self._connection() as connection, connection.transaction():
            candidate = await traced_call(
                "tool",
                "similarity_repository",
                request,
                lambda: SimilarityRepository.detect_and_store(
                    connection, request.invoice_id, vector, model_version, self._config
                ),
            )
            result = SimilarityResult(
                status="NEAR_DUPLICATE" if candidate is not None else "NO_MATCH",
                candidate=candidate,
                extraction_sha256=model_digest(request.extraction),
                embedding_sha256=model_digest(vector),
                config_sha256=model_digest(self._config),
                model_version=model_version,
                gateway_model=embedded.provenance.model,
                input_tokens=embedded.usage.input_tokens,
                output_tokens=embedded.usage.output_tokens,
                gateway_latency_ms=embedded.latency_ms,
                gateway_cost_usd=embedded.cost_usd,
                config=self._config,
            )
            await self._audit_writer.append(
                connection,
                AppendEvent(
                    run_id=request.run_id,
                    invoice_id=request.invoice_id,
                    event_type="similarity.completed",
                    node="NearDuplicate",
                    actor_type="AGENT",
                    actor_id="invoiceops-near-duplicate",
                    versions=VersionOverrides(
                        model_version=model_version,
                        prompt_version=SUMMARY_VERSION,
                        policy_version=self._config.version,
                    ),
                    payload=result.model_dump(mode="json"),
                ),
                trace_id=request.trace_id,
            )
        logger.info(
            "similarity_completed run_id=%s invoice_id=%s trace_id=%s status=%s "
            "model_version=%s policy_version=%s duration_ms=%.3f",
            request.run_id,
            request.invoice_id,
            request.trace_id,
            result.status,
            model_version,
            self._config.version,
            (perf_counter() - started) * 1000,
        )
        return result

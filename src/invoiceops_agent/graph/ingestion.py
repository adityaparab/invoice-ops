"""Orchestrate raw persistence and an atomic invoice/run/audit/idempotency transaction."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from time import perf_counter
from typing import Protocol
from uuid import UUID, uuid4

import psycopg
from pydantic import ValidationError

from invoiceops_agent.ledger.audit import AuditWriter
from invoiceops_agent.ledger.errors import LedgerError
from invoiceops_agent.ledger.schemas import AppendEvent, VersionOverrides
from invoiceops_agent.ledger.settings import LedgerSettings
from invoiceops_agent.ledger.writer import LedgerWriter
from invoiceops_agent.tools.ingestion_errors import IngestionUnavailable
from invoiceops_agent.tools.ingestion_repository import IngestionRepository
from invoiceops_agent.tools.ingestion_schemas import IngestionOutcome, IngestionResult, RawDocument
from invoiceops_agent.tools.raw_storage import RawStorage
from invoiceops_agent.tools.webhook_auth import WebhookNonce

logger = logging.getLogger(__name__)
INGESTION_VERSION = "invoice-v1"


def utc_now() -> datetime:
    return datetime.now(UTC)


class UploadService(Protocol):
    async def ingest(
        self,
        document: RawDocument,
        *,
        key: str,
        trace_id: str,
        nonce: WebhookNonce | None = None,
    ) -> IngestionOutcome: ...


class IngestionService:
    def __init__(
        self,
        repository: IngestionRepository,
        storage: RawStorage,
        ledger: AuditWriter | None = None,
        *,
        clock: Callable[[], datetime] = utc_now,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self.repository = repository
        self.storage = storage
        self.ledger = (
            ledger
            if ledger is not None
            else LedgerWriter(
                LedgerSettings(
                    graph_version=INGESTION_VERSION,
                    model_version="not-applicable@v1",
                    prompt_version="not-applicable@v1",
                    policy_version="not-applicable@v1",
                ),
                clock=clock,
                new_id=new_id,
            )
        )
        self.clock = clock
        self.new_id = new_id

    async def ingest(
        self,
        document: RawDocument,
        *,
        key: str,
        trace_id: str,
        nonce: WebhookNonce | None = None,
    ) -> IngestionOutcome:
        if (document.source == "EMAIL") != (nonce is not None):
            raise ValueError("Email ingestion requires a nonce; upload ingestion forbids one")
        started = perf_counter()
        run_id: UUID | None = None
        try:
            if nonce is None:
                async with asyncio.timeout(10), self.repository.connection() as connection:
                    replay = await self.repository.replay(connection, key, document.request_hash)
                if replay is not None:
                    logger.info(
                        "ingest_replayed run_id=%s trace_id=%s", replay.body.run_id, trace_id
                    )
                    return replay
            raw_ref = await self.storage.put(document, trace_id=trace_id)
            result = IngestionResult(invoice_id=self.new_id(), run_id=self.new_id())
            run_id = result.run_id
            created_at = self.clock()
            if created_at.utcoffset() is None:
                raise ValueError("Ingestion clock must return timezone-aware UTC timestamps")
            async with asyncio.timeout(10), self.repository.connection() as connection:
                async with connection.transaction():
                    await self.repository.lock_key(connection, key)
                    if nonce is not None:
                        await self.repository.claim_nonce(connection, nonce, created_at=created_at)
                    replay = await self.repository.replay(connection, key, document.request_hash)
                    if replay is not None:
                        logger.info(
                            "ingest_replayed run_id=%s trace_id=%s", replay.body.run_id, trace_id
                        )
                        return replay
                    created = await self.repository.create(
                        connection,
                        result,
                        document,
                        raw_ref=raw_ref,
                        graph_version=INGESTION_VERSION,
                        trace_id=trace_id,
                        created_at=created_at,
                    )
                    versions: VersionOverrides | None = None
                    if not created:
                        original = await self.repository.original(connection, document.content_hash)
                        result = original.response_body.model_copy(update={"duplicate": True})
                        run_id = result.run_id
                        raw_ref = original.raw_ref
                        versions = VersionOverrides(
                            graph_version=original.graph_version,
                            model_version="not-applicable@v1",
                            prompt_version="not-applicable@v1",
                            policy_version="not-applicable@v1",
                        )
                    outcome = IngestionOutcome(response_status=201 if created else 200, body=result)
                    await self.ledger.append(
                        connection,
                        AppendEvent(
                            run_id=result.run_id,
                            invoice_id=result.invoice_id,
                            event_type="ingest.accepted"
                            if created
                            else "ingest.duplicate_rejected",
                            node="Ingest" if created else "Reject",
                            actor_type="SYSTEM",
                            actor_id="invoiceops-ingestion",
                            payload={
                                "content_hash": document.content_hash,
                                "raw_ref": raw_ref,
                                "source": document.source,
                                "content_type": document.content_type,
                                "size_bytes": len(document.body),
                                **(
                                    {"route": "REJECT", "reason": "DUP_EXACT"}
                                    if not created
                                    else {}
                                ),
                            },
                            versions=versions,
                        ),
                        trace_id=trace_id,
                    )
                    await self.repository.remember(connection, key, document, outcome, created_at)
        except (psycopg.Error, LedgerError, ValidationError, TimeoutError) as error:
            logger.error(
                "ingest_failed run_id=%s trace_id=%s error_type=%s",
                run_id,
                trace_id,
                type(error).__name__,
            )
            raise IngestionUnavailable("Invoice persistence is unavailable.") from error
        logger.info(
            "ingest_committed run_id=%s invoice_id=%s trace_id=%s duration_ms=%.3f",
            result.run_id,
            result.invoice_id,
            trace_id,
            (perf_counter() - started) * 1000,
        )
        return outcome

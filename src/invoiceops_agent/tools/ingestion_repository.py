"""Short async Postgres transactions and durable successful-request replay."""

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

import psycopg
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from invoiceops_agent.ledger.connection import LedgerConnection
from invoiceops_agent.tools.ingestion_errors import (
    IdempotencyConflict,
    IngestionUnavailable,
    WebhookNonceReuse,
)
from invoiceops_agent.tools.ingestion_schemas import (
    IngestionOutcome,
    IngestionResult,
    OriginalIngestion,
    RawDocument,
)
from invoiceops_agent.tools.webhook_auth import WebhookNonce


class IngestionRepository:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[psycopg.AsyncConnection[DictRow]]:
        async with await psycopg.AsyncConnection.connect(
            self.dsn,
            row_factory=dict_row,
            autocommit=True,
            connect_timeout=5,
            options="-c timezone=UTC -c statement_timeout=5000 -c lock_timeout=2000",
        ) as connection:
            yield connection

    @staticmethod
    async def replay(
        connection: LedgerConnection, key: str, request_hash: str
    ) -> IngestionOutcome | None:
        cursor = await connection.execute(
            "SELECT request_hash, response_status, response_body FROM public.ingestion_requests "
            "WHERE idempotency_key = %s",
            (key,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise IdempotencyConflict("Idempotency key was already used for a different request.")
        return IngestionOutcome.model_validate(
            {"response_status": row["response_status"], "body": row["response_body"]}
        )

    @staticmethod
    async def lock_key(connection: LedgerConnection, key: str) -> None:
        lock = int.from_bytes(hashlib.sha256(key.encode("ascii")).digest()[:8], signed=True)
        await connection.execute("SELECT pg_advisory_xact_lock(%s)", (lock,))

    @staticmethod
    async def claim_nonce(
        connection: LedgerConnection, nonce: WebhookNonce, *, created_at: datetime
    ) -> None:
        cursor = await connection.execute(
            "INSERT INTO public.webhook_nonces (nonce, signed_at, created_at) "
            "VALUES (%s, %s, %s) ON CONFLICT (nonce) DO NOTHING RETURNING nonce",
            (nonce.nonce, nonce.signed_at, created_at),
        )
        if await cursor.fetchone() is None:
            raise WebhookNonceReuse("This webhook nonce has already been used.")

    @staticmethod
    async def create(
        connection: LedgerConnection,
        result: IngestionResult,
        document: RawDocument,
        *,
        raw_ref: str,
        graph_version: str,
        trace_id: str,
        created_at: datetime,
    ) -> bool:
        cursor = await connection.execute(
            "INSERT INTO public.invoices "
            "(id, content_hash, raw_ref, content_type, source, status, created_at) "
            "VALUES (%s, %s, %s, %s, %s, 'QUEUED', %s) "
            "ON CONFLICT (content_hash) DO NOTHING RETURNING id",
            (
                result.invoice_id,
                document.content_hash,
                raw_ref,
                document.content_type,
                document.source,
                created_at,
            ),
        )
        if await cursor.fetchone() is None:
            return False
        await connection.execute(
            "INSERT INTO public.runs "
            "(id, invoice_id, status, graph_version, trace_id, created_at) "
            "VALUES (%s, %s, 'QUEUED', %s, %s, %s)",
            (result.run_id, result.invoice_id, graph_version, trace_id, created_at),
        )
        return True

    @staticmethod
    async def original(connection: LedgerConnection, content_hash: str) -> OriginalIngestion:
        # The conflicting insert waits for the creator's atomic transaction. A new READ COMMITTED
        # statement then sees its committed invoice, initial run, and original 201 response.
        cursor = await connection.execute(
            "SELECT request.response_body, runs.graph_version, invoices.raw_ref "
            "FROM public.invoices JOIN public.runs ON runs.invoice_id = invoices.id "
            "JOIN public.ingestion_requests request "
            "ON request.run_id = runs.id AND request.invoice_id = invoices.id "
            "WHERE invoices.content_hash = %s AND request.response_status = 201 "
            "ORDER BY request.created_at, request.idempotency_key LIMIT 1 FOR UPDATE OF invoices",
            (content_hash,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise IngestionUnavailable("The original ingestion identity is unavailable.")
        return OriginalIngestion.model_validate(row)

    @staticmethod
    async def remember(
        connection: LedgerConnection,
        key: str,
        document: RawDocument,
        outcome: IngestionOutcome,
        created_at: datetime,
    ) -> None:
        await connection.execute(
            "INSERT INTO public.ingestion_requests (idempotency_key, request_hash, invoice_id, "
            "run_id, response_status, response_body, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                key,
                document.request_hash,
                outcome.body.invoice_id,
                outcome.body.run_id,
                outcome.response_status,
                Jsonb(outcome.body.model_dump(mode="json")),
                created_at,
            ),
        )

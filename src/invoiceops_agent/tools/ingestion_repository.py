"""Short async Postgres transactions and durable successful-request replay."""

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

import psycopg
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from invoiceops_agent.ledger.connection import LedgerConnection
from invoiceops_agent.tools.ingestion_errors import IdempotencyConflict
from invoiceops_agent.tools.ingestion_schemas import IngestionResult, RawDocument


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
    ) -> IngestionResult | None:
        cursor = await connection.execute(
            "SELECT request_hash, response_body FROM public.ingestion_requests "
            "WHERE idempotency_key = %s",
            (key,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise IdempotencyConflict("Idempotency key was already used for a different request.")
        return IngestionResult.model_validate(row["response_body"])

    @staticmethod
    async def lock_key(connection: LedgerConnection, key: str) -> None:
        lock = int.from_bytes(hashlib.sha256(key.encode("ascii")).digest()[:8], signed=True)
        await connection.execute("SELECT pg_advisory_xact_lock(%s)", (lock,))

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
    ) -> None:
        await connection.execute(
            "INSERT INTO public.invoices "
            "(id, content_hash, raw_ref, content_type, source, status, created_at) "
            "VALUES (%s, %s, %s, %s, 'UPLOAD', 'QUEUED', %s)",
            (result.invoice_id, document.content_hash, raw_ref, document.content_type, created_at),
        )
        await connection.execute(
            "INSERT INTO public.runs "
            "(id, invoice_id, status, graph_version, trace_id, created_at) "
            "VALUES (%s, %s, 'QUEUED', %s, %s, %s)",
            (result.run_id, result.invoice_id, graph_version, trace_id, created_at),
        )

    @staticmethod
    async def remember(
        connection: LedgerConnection,
        key: str,
        document: RawDocument,
        result: IngestionResult,
        created_at: datetime,
    ) -> None:
        await connection.execute(
            "INSERT INTO public.ingestion_requests (idempotency_key, request_hash, invoice_id, "
            "run_id, response_status, response_body, created_at) VALUES (%s,%s,%s,%s,201,%s,%s)",
            (
                key,
                document.request_hash,
                result.invoice_id,
                result.run_id,
                Jsonb(result.model_dump(mode="json")),
                created_at,
            ),
        )

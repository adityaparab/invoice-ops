"""Transactional audit history under real runtime-role grants and concurrent writers."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row
from pydantic import SecretStr
from sqlalchemy.engine import URL
from tests.integration.support import INVOICE_ID, RUN_ID, seed_invoice_and_run

from invoiceops_agent.db.runtime_role import provision_runtime_login
from invoiceops_agent.db.settings import ProvisioningSettings
from invoiceops_agent.ledger.errors import LedgerConflict, LedgerIdentityMismatch
from invoiceops_agent.ledger.reader import LedgerReader
from invoiceops_agent.ledger.schemas import AppendEvent, VersionOverrides
from invoiceops_agent.ledger.settings import LedgerSettings
from invoiceops_agent.ledger.writer import LedgerWriter

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
TRACE_ID = "a" * 32
CREATED_AT = datetime(2026, 9, 23, 12, tzinfo=UTC)


@pytest.fixture
def ledger_runtime_dsn(
    migrated_database: psycopg.Connection[tuple[object, ...]], migration_dsn: str
) -> str:
    seed_invoice_and_run(migrated_database)
    password = "synthetic-ledger-runtime-password"
    provision_runtime_login(
        ProvisioningSettings(
            migration_dsn=SecretStr(migration_dsn), app_password=SecretStr(password)
        )
    )
    info = migrated_database.info
    return URL.create(
        "postgresql",
        username="invoiceops_app",
        password=password,
        host=info.host,
        port=info.port,
        database=info.dbname,
    ).render_as_string(hide_password=False)


@asynccontextmanager
async def runtime_connection(dsn: str) -> AsyncIterator[psycopg.AsyncConnection[DictRow]]:
    async with await psycopg.AsyncConnection.connect(
        dsn,
        autocommit=True,
        row_factory=dict_row,
        connect_timeout=5,
        options="-c statement_timeout=5000 -c lock_timeout=5000",
    ) as connection:
        yield connection


def writer(*, event_id: UUID | None = None) -> LedgerWriter:
    settings = LedgerSettings(
        graph_version="graph@v1",
        model_version="not-applicable@v1",
        prompt_version="not-applicable@v1",
        policy_version="not-applicable@v1",
    )
    if event_id is not None:
        return LedgerWriter(settings, clock=lambda: CREATED_AT, new_id=lambda: event_id)
    return LedgerWriter(settings, clock=lambda: CREATED_AT)


def command(**changes: object) -> AppendEvent:
    return AppendEvent.model_validate(
        {
            "run_id": RUN_ID,
            "invoice_id": INVOICE_ID,
            "event_type": "ingest.accepted",
            "node": "Ingest",
            "actor_type": "SYSTEM",
            "actor_id": "synthetic-ingestion-service",
            "payload": {"content_hash": "a" * 64},
            **changes,
        }
    )


async def test_runtime_writer_commits_with_replay_response_and_reader_returns_all_pins(
    ledger_runtime_dsn: str,
) -> None:
    async with runtime_connection(ledger_runtime_dsn) as connection:
        async with connection.transaction():
            event = await writer().append(connection, command(), trace_id=TRACE_ID)
            await connection.execute(
                "INSERT INTO public.ingestion_requests (idempotency_key, request_hash, invoice_id, "
                "run_id, response_status, response_body) "
                "VALUES ('ledger-test', %s, %s, %s, 201, '{}')",
                ("a" * 64, INVOICE_ID, RUN_ID),
            )
    async with runtime_connection(ledger_runtime_dsn) as reader_connection:
        page = await LedgerReader().for_run(reader_connection, RUN_ID, trace_id=TRACE_ID)
        assert page.events == [event]
        assert page.events[0].versions.model_version == "not-applicable@v1"
        cursor = await reader_connection.execute(
            "SELECT count(*) AS n FROM public.ingestion_requests"
        )
        assert await cursor.fetchone() == {"n": 1}


async def test_caller_rollback_removes_business_rows_and_staged_ledger_together(
    ledger_runtime_dsn: str,
) -> None:
    new_invoice, new_run = UUID(int=90), UUID(int=91)
    async with runtime_connection(ledger_runtime_dsn) as connection:
        with pytest.raises(RuntimeError, match="rollback business mutation"):
            async with connection.transaction():
                await connection.execute(
                    "INSERT INTO public.invoices (id, content_hash, raw_ref, content_type, source) "
                    "VALUES (%s, %s, 'sha256/test', 'application/pdf', 'UPLOAD')",
                    (new_invoice, "b" * 64),
                )
                await connection.execute(
                    "INSERT INTO public.runs (id, invoice_id, graph_version, trace_id) "
                    "VALUES (%s, %s, 'graph@v1', %s)",
                    (new_run, new_invoice, TRACE_ID),
                )
                await writer().append(
                    connection, command(run_id=new_run, invoice_id=new_invoice), trace_id=TRACE_ID
                )
                raise RuntimeError("rollback business mutation")
        cursor = await connection.execute(
            "SELECT count(*) AS n FROM public.invoices WHERE id=%s", (new_invoice,)
        )
        assert await cursor.fetchone() == {"n": 0}
        page = await LedgerReader().for_run(connection, new_run, trace_id=TRACE_ID)
        assert page.events == []


async def test_concurrent_runtime_writers_allocate_contiguous_unique_sequences(
    ledger_runtime_dsn: str,
) -> None:
    async def append_one(index: int) -> int:
        async with runtime_connection(ledger_runtime_dsn) as connection, connection.transaction():
            event = await writer(event_id=UUID(int=index + 30)).append(
                connection, command(), trace_id=TRACE_ID
            )
            return event.sequence

    sequences = await asyncio.gather(*(append_one(index) for index in range(8)))
    assert sorted(sequences) == list(range(1, 9))
    async with runtime_connection(ledger_runtime_dsn) as connection:
        reader = LedgerReader()
        first = await reader.for_run(connection, RUN_ID, trace_id=TRACE_ID, limit=3)
        assert [event.sequence for event in first.events] == [1, 2, 3]
        second = await reader.for_run(
            connection, RUN_ID, trace_id=TRACE_ID, limit=3, after=first.next_cursor
        )
        assert [event.sequence for event in second.events] == [4, 5, 6]
        third = await reader.for_run(
            connection, RUN_ID, trace_id=TRACE_ID, limit=3, after=second.next_cursor
        )
        assert [event.sequence for event in third.events] == [7, 8]
        assert third.next_cursor is None


async def test_invoice_reader_crosses_runs_and_handles_equal_timestamp_ties(
    ledger_runtime_dsn: str,
) -> None:
    ids = [UUID(int=40), UUID(int=41), UUID(int=42)]
    async with runtime_connection(ledger_runtime_dsn) as connection:
        async with connection.transaction():
            for index, event_id in enumerate(ids):
                run_id = UUID(int=100 + index)
                await connection.execute(
                    "INSERT INTO public.runs (id, invoice_id, graph_version, trace_id) "
                    "VALUES (%s, %s, 'graph@v1', %s)",
                    (run_id, INVOICE_ID, TRACE_ID),
                )
                await writer(event_id=event_id).append(
                    connection, command(run_id=run_id), trace_id=TRACE_ID
                )
        reader = LedgerReader()
        first = await reader.for_invoice(connection, INVOICE_ID, trace_id=TRACE_ID, limit=2)
        assert [event.id for event in first.events] == ids[:2]
        second = await reader.for_invoice(
            connection, INVOICE_ID, trace_id=TRACE_ID, limit=2, after=first.next_cursor
        )
        assert [event.id for event in second.events] == ids[2:]
        assert second.next_cursor is None
        assert len({event.run_id for event in first.events + second.events}) == 3


async def test_supersession_preserves_original_payload_and_provenance(
    ledger_runtime_dsn: str,
) -> None:
    async with runtime_connection(ledger_runtime_dsn) as connection:
        async with connection.transaction():
            original = await writer().append(connection, command(), trace_id=TRACE_ID)
            correction = await writer().append(
                connection,
                command(
                    event_type="ingest.corrected",
                    supersedes_id=original.id,
                    payload={"reason": "synthetic correction"},
                    versions=VersionOverrides(policy_version="policy@v2"),
                ),
                trace_id=TRACE_ID,
            )
        page = await LedgerReader().for_run(connection, RUN_ID, trace_id=TRACE_ID)
    assert page.events == [original, correction]
    assert original.payload == {"content_hash": "a" * 64}
    assert original.versions.policy_version == "not-applicable@v1"
    assert correction.versions.policy_version == "policy@v2"
    assert correction.sequence == 2


async def test_identity_rejection_and_event_id_collision_leave_no_partial_event(
    ledger_runtime_dsn: str,
) -> None:
    fixed_writer = writer(event_id=UUID(int=50))
    async with runtime_connection(ledger_runtime_dsn) as connection:
        with pytest.raises(LedgerIdentityMismatch):
            async with connection.transaction():
                await fixed_writer.append(
                    connection, command(invoice_id=UUID(int=999)), trace_id=TRACE_ID
                )
        async with connection.transaction():
            await fixed_writer.append(connection, command(), trace_id=TRACE_ID)
        with pytest.raises(LedgerConflict):
            async with connection.transaction():
                await fixed_writer.append(connection, command(), trace_id=TRACE_ID)
        async with connection.transaction():
            next_event = await writer().append(connection, command(), trace_id=TRACE_ID)
        page = await LedgerReader().for_run(connection, RUN_ID, trace_id=TRACE_ID)
        assert [event.sequence for event in page.events] == [1, 2]
        assert next_event.sequence == 2

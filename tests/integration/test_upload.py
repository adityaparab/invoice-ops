"""Real restricted-role transactions and MinIO objects through the async HTTP boundary."""

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

import psycopg
import pytest
from minio import Minio
from tests.integration.conftest import TEST_PASSWORD, TEST_USER
from tests.unit.test_api import assert_problem, client_for
from tests.unit.test_upload import HEADERS, PDF

from invoiceops_agent.api.app import create_app
from invoiceops_agent.api.ingestion_dependencies import UploadFactory
from invoiceops_agent.api.settings import ApiSettings
from invoiceops_agent.graph.ingestion import INGESTION_VERSION, IngestionService, UploadService
from invoiceops_agent.ledger.connection import LedgerConnection
from invoiceops_agent.ledger.errors import LedgerStorageError
from invoiceops_agent.ledger.schemas import AppendEvent, LedgerEvent
from invoiceops_agent.ledger.settings import LedgerSettings
from invoiceops_agent.ledger.writer import LedgerWriter
from invoiceops_agent.tools.ingestion_repository import IngestionRepository
from invoiceops_agent.tools.ingestion_schemas import IngestionOutcome, IngestionResult, RawDocument
from invoiceops_agent.tools.raw_storage import s3_storage
from invoiceops_agent.tools.storage_bootstrap import provision_bucket

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
FIXED_TIME = datetime(2026, 1, 2, tzinfo=UTC)


def row_counts(connection: psycopg.Connection[tuple[object, ...]]) -> tuple[object, ...] | None:
    return connection.execute(
        "SELECT (SELECT count(*) FROM invoices), (SELECT count(*) FROM runs), "
        "(SELECT count(*) FROM ledger), (SELECT count(*) FROM ingestion_requests)"
    ).fetchone()


async def test_accepts_and_retrieves_identical_raw_bytes(
    upload_settings: ApiSettings,
    migrated_database: psycopg.Connection[tuple[object, ...]],
    minio_client: Minio,
) -> None:
    async with client_for(create_app(upload_settings)) as client:
        response = await client.post(
            "/v1/invoices",
            headers=HEADERS,
            files={"file": ("synthetic.pdf", PDF, "application/pdf")},
        )
    assert response.status_code == 201
    result = IngestionResult.model_validate(response.json())
    assert row_counts(migrated_database) == (1, 1, 1, 1)
    content_hash = hashlib.sha256(PDF).hexdigest()
    raw = minio_client.get_object("invoiceops-raw", f"sha256/{content_hash[:2]}/{content_hash}")
    try:
        assert raw.read() == PDF
    finally:
        raw.close()
        raw.release_conn()
    row = migrated_database.execute(
        "SELECT i.status,r.status,l.event_type,l.actor_type,l.graph_version,l.model_version, "
        "l.prompt_version,l.policy_version,l.payload FROM invoices i "
        "JOIN runs r ON r.invoice_id=i.id "
        "JOIN ledger l ON l.run_id=r.id WHERE r.id=%s",
        (result.run_id,),
    ).fetchone()
    assert row is not None
    assert row[:8] == (
        "QUEUED",
        "QUEUED",
        "ingest.accepted",
        "SYSTEM",
        INGESTION_VERSION,
        "not-applicable@v1",
        "not-applicable@v1",
        "not-applicable@v1",
    )
    assert isinstance(row[8], dict) and row[8]["size_bytes"] == len(PDF)


async def test_replay_conflict_and_duplicate_preserve_original_ids(
    upload_settings: ApiSettings,
    migrated_database: psycopg.Connection[tuple[object, ...]],
) -> None:
    async with client_for(create_app(upload_settings)) as client:
        first = await client.post(
            "/v1/invoices", headers=HEADERS, files={"file": ("one.pdf", PDF, "application/pdf")}
        )
        replay = await client.post(
            "/v1/invoices", headers=HEADERS, files={"file": ("renamed.pdf", PDF, "application/pdf")}
        )
        conflict = await client.post(
            "/v1/invoices",
            headers=HEADERS,
            files={"file": ("other.pdf", PDF + b"changed", "application/pdf")},
        )
        duplicate = await client.post(
            "/v1/invoices",
            headers={**HEADERS, "Idempotency-Key": "new"},
            files={"file": ("same.pdf", PDF, "application/pdf")},
        )
        duplicate_replay = await client.post(
            "/v1/invoices",
            headers={**HEADERS, "Idempotency-Key": "new"},
            files={"file": ("same.pdf", PDF, "application/pdf")},
        )
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert_problem(conflict, 409)
    assert duplicate.status_code == duplicate_replay.status_code == 200
    assert duplicate.json() == duplicate_replay.json() == {**first.json(), "duplicate": True}
    async with client_for(
        create_app(upload_settings.model_copy(update={"raw_bucket": "missing-bucket"}))
    ) as client:
        replay_without_storage = await client.post(
            "/v1/invoices",
            headers=HEADERS,
            files={"file": ("one.pdf", PDF, "application/pdf")},
        )
    assert replay_without_storage.status_code == 201
    assert replay_without_storage.json() == first.json()
    assert row_counts(migrated_database) == (1, 1, 2, 2)
    duplicate_event = migrated_database.execute(
        "SELECT event_type,node,actor_type,payload,graph_version,model_version,prompt_version,"
        "policy_version FROM ledger WHERE sequence=2"
    ).fetchone()
    assert duplicate_event is not None
    assert duplicate_event[:3] == ("ingest.duplicate_rejected", "Reject", "SYSTEM")
    assert isinstance(duplicate_event[3], dict) and duplicate_event[3]["route"] == "REJECT"
    assert duplicate_event[4:] == (
        INGESTION_VERSION,
        "not-applicable@v1",
        "not-applicable@v1",
        "not-applicable@v1",
    )


@pytest.mark.parametrize("same_payload", [True, False])
async def test_concurrent_same_key_is_serialized(
    upload_settings: ApiSettings,
    migrated_database: psycopg.Connection[tuple[object, ...]],
    same_payload: bool,
) -> None:
    async with client_for(create_app(upload_settings)) as client:
        responses = await asyncio.gather(
            *(
                client.post(
                    "/v1/invoices",
                    headers=HEADERS,
                    files={
                        "file": (
                            "file.pdf",
                            PDF if same_payload else PDF + str(i).encode(),
                            "application/pdf",
                        )
                    },
                )
                for i in range(4)
            )
        )
    assert sorted(response.status_code for response in responses) == (
        [201] * 4 if same_payload else [201, 409, 409, 409]
    )
    if same_payload:
        assert all(response.json() == responses[0].json() for response in responses)
    assert row_counts(migrated_database) == (1, 1, 1, 1)


async def test_storage_failure_persists_nothing(
    upload_settings: ApiSettings,
    migrated_database: psycopg.Connection[tuple[object, ...]],
) -> None:
    settings = upload_settings.model_copy(update={"raw_bucket": "missing-bucket"})
    async with client_for(create_app(settings)) as client:
        response = await client.post(
            "/v1/invoices", headers=HEADERS, files={"file": ("a.pdf", PDF, "application/pdf")}
        )
    assert_problem(response, 503)
    assert row_counts(migrated_database) == (0, 0, 0, 0)


class FailingLedger(LedgerWriter):
    async def append(
        self, connection: LedgerConnection, command: AppendEvent, *, trace_id: str
    ) -> LedgerEvent:
        await super().append(connection, command, trace_id=trace_id)
        raise LedgerStorageError(
            "synthetic-private-failure", run_id=command.run_id, trace_id=trace_id
        )


class FailingRepository(IngestionRepository):
    @staticmethod
    async def remember(
        connection: LedgerConnection,
        key: str,
        document: RawDocument,
        result: IngestionOutcome,
        created_at: datetime,
    ) -> None:
        await IngestionRepository.remember(connection, key, document, result, created_at)
        raise psycopg.OperationalError("synthetic-private-failure")


def failure_factory(failure: Literal["ledger", "replay"]) -> UploadFactory:
    @asynccontextmanager
    async def factory(settings: ApiSettings) -> AsyncIterator[UploadService | None]:
        assert settings.postgres_dsn is not None and settings.minio_url is not None
        async with s3_storage(
            endpoint=str(settings.minio_url),
            access_key=TEST_USER,
            secret_key=TEST_PASSWORD,
            bucket=settings.raw_bucket,
            timeout_seconds=5,
        ) as storage:
            repository = (FailingRepository if failure == "replay" else IngestionRepository)(
                settings.postgres_dsn.get_secret_value()
            )
            writer = (FailingLedger if failure == "ledger" else LedgerWriter)(
                LedgerSettings(
                    graph_version=INGESTION_VERSION,
                    model_version="not-applicable@v1",
                    prompt_version="not-applicable@v1",
                    policy_version="not-applicable@v1",
                )
            )
            yield IngestionService(repository, storage, writer, clock=lambda: FIXED_TIME)

    return factory


@pytest.mark.parametrize("failure", ["ledger", "replay"])
async def test_failure_rolls_back_all_rows_without_deleting_shared_raw(
    upload_settings: ApiSettings,
    migrated_database: psycopg.Connection[tuple[object, ...]],
    minio_client: Minio,
    failure: Literal["ledger", "replay"],
    caplog: pytest.LogCaptureFixture,
) -> None:

    async with client_for(
        create_app(upload_settings, upload_factory=failure_factory(failure))
    ) as client:
        response = await client.post(
            "/v1/invoices", headers=HEADERS, files={"file": ("a.pdf", PDF, "application/pdf")}
        )
    assert_problem(response, 503)
    assert row_counts(migrated_database) == (0, 0, 0, 0)
    assert len(list(minio_client.list_objects("invoiceops-raw", recursive=True))) == 1
    assert "synthetic-private-failure" not in response.text + caplog.text
    # The orphaned content object is safe to reuse on a successful retry.
    async with client_for(create_app(upload_settings)) as client:
        retry = await client.post(
            "/v1/invoices", headers=HEADERS, files={"file": ("a.pdf", PDF, "application/pdf")}
        )
    assert retry.status_code == 201
    assert row_counts(migrated_database) == (1, 1, 1, 1)


async def test_auth_failure_writes_nothing_and_bucket_bootstrap_is_repeatable(
    upload_settings: ApiSettings,
    migrated_database: psycopg.Connection[tuple[object, ...]],
    minio_client: Minio,
) -> None:
    await provision_bucket(upload_settings)
    await provision_bucket(upload_settings)
    async with client_for(create_app(upload_settings)) as client:
        response = await client.post(
            "/v1/invoices",
            headers={"Idempotency-Key": "no-auth"},
            files={"file": ("a.pdf", PDF, "application/pdf")},
        )
    assert_problem(response, 401)
    assert row_counts(migrated_database) == (0, 0, 0, 0)
    assert not list(minio_client.list_objects("invoiceops-raw", recursive=True))


@pytest.mark.parametrize("already_exists", [False, True])
async def test_concurrent_different_keys_have_one_invoice_and_one_event_per_key(
    upload_settings: ApiSettings,
    migrated_database: psycopg.Connection[tuple[object, ...]],
    already_exists: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_create = IngestionRepository.create
    all_started = asyncio.Event()
    started = 0

    async def concurrent_create(
        connection: LedgerConnection,
        result: IngestionResult,
        document: RawDocument,
        *,
        raw_ref: str,
        graph_version: str,
        trace_id: str,
        created_at: datetime,
    ) -> bool:
        nonlocal started
        started += 1
        if started == 4:
            all_started.set()
        async with asyncio.timeout(5):
            await all_started.wait()
        return await original_create(
            connection,
            result,
            document,
            raw_ref=raw_ref,
            graph_version=graph_version,
            trace_id=trace_id,
            created_at=created_at,
        )

    async with client_for(create_app(upload_settings)) as client:
        if already_exists:
            assert (
                await client.post(
                    "/v1/invoices",
                    headers=HEADERS,
                    files={"file": ("a.pdf", PDF, "application/pdf")},
                )
            ).status_code == 201
        monkeypatch.setattr(IngestionRepository, "create", staticmethod(concurrent_create))
        responses = await asyncio.gather(
            *(
                client.post(
                    "/v1/invoices",
                    headers={**HEADERS, "Idempotency-Key": f"parallel-{index}"},
                    files={"file": ("a.pdf", PDF, "application/pdf")},
                )
                for index in range(4)
            )
        )
    assert started == 4
    assert sorted(response.status_code for response in responses) == (
        [200, 200, 200, 200] if already_exists else [200, 200, 200, 201]
    )
    assert len({response.json()["invoice_id"] for response in responses}) == 1
    assert len({response.json()["run_id"] for response in responses}) == 1
    count = 5 if already_exists else 4
    assert row_counts(migrated_database) == (1, 1, count, count)
    sequences = migrated_database.execute(
        "SELECT sequence FROM ledger ORDER BY sequence"
    ).fetchall()
    assert sequences == [(sequence,) for sequence in range(1, count + 1)]


async def test_concurrent_replay_of_duplicate_key_emits_one_reject_event(
    upload_settings: ApiSettings,
    migrated_database: psycopg.Connection[tuple[object, ...]],
) -> None:
    async with client_for(create_app(upload_settings)) as client:
        original = await client.post(
            "/v1/invoices", headers=HEADERS, files={"file": ("a.pdf", PDF, "application/pdf")}
        )
        responses = await asyncio.gather(
            *(
                client.post(
                    "/v1/invoices",
                    headers={**HEADERS, "Idempotency-Key": "duplicate-key"},
                    files={"file": ("a.pdf", PDF, "application/pdf")},
                )
                for _ in range(4)
            )
        )
        conflict = await client.post(
            "/v1/invoices",
            headers={**HEADERS, "Idempotency-Key": "duplicate-key"},
            files={"file": ("a.pdf", PDF + b"changed", "application/pdf")},
        )
    assert original.status_code == 201
    assert all(response.status_code == 200 for response in responses)
    assert all(response.json() == {**original.json(), "duplicate": True} for response in responses)
    assert_problem(conflict, 409)
    assert row_counts(migrated_database) == (1, 1, 2, 2)


@pytest.mark.parametrize("failure", ["ledger", "replay"])
async def test_duplicate_failure_rolls_back_reject_event_and_key_reservation(
    upload_settings: ApiSettings,
    migrated_database: psycopg.Connection[tuple[object, ...]],
    minio_client: Minio,
    failure: Literal["ledger", "replay"],
) -> None:
    async with client_for(create_app(upload_settings)) as client:
        original = await client.post(
            "/v1/invoices", headers=HEADERS, files={"file": ("a.pdf", PDF, "application/pdf")}
        )
    headers = {**HEADERS, "Idempotency-Key": "failed-duplicate"}
    async with client_for(
        create_app(upload_settings, upload_factory=failure_factory(failure))
    ) as client:
        failed = await client.post(
            "/v1/invoices", headers=headers, files={"file": ("a.pdf", PDF, "application/pdf")}
        )
    assert_problem(failed, 503)
    assert row_counts(migrated_database) == (1, 1, 1, 1)
    assert len(list(minio_client.list_objects("invoiceops-raw", recursive=True))) == 1
    async with client_for(create_app(upload_settings)) as client:
        retried = await client.post(
            "/v1/invoices", headers=headers, files={"file": ("a.pdf", PDF, "application/pdf")}
        )
    assert retried.status_code == 200
    assert retried.json() == {**original.json(), "duplicate": True}
    assert row_counts(migrated_database) == (1, 1, 2, 2)


async def test_duplicate_resolves_original_run_and_preserves_processing_state(
    upload_settings: ApiSettings,
    migrated_database: psycopg.Connection[tuple[object, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as earlier_version:
        earlier_version.setattr(
            "invoiceops_agent.graph.ingestion.INGESTION_VERSION", "ingestion-old-v1"
        )
        async with client_for(create_app(upload_settings)) as client:
            original = await client.post(
                "/v1/invoices", headers=HEADERS, files={"file": ("a.pdf", PDF, "application/pdf")}
            )
    body = original.json()
    migrated_database.execute("UPDATE invoices SET status='PROCESSING'")
    migrated_database.execute("UPDATE runs SET status='RUNNING'")
    migrated_database.execute(
        "INSERT INTO runs (id,invoice_id,graph_version,trace_id) VALUES(%s,%s,'later-v2',%s)",
        (UUID(int=999), UUID(body["invoice_id"]), "a" * 32),
    )
    async with client_for(create_app(upload_settings)) as client:
        duplicate = await client.post(
            "/v1/invoices",
            headers={**HEADERS, "Idempotency-Key": "duplicate-after-processing"},
            files={"file": ("a.pdf", PDF, "application/pdf")},
        )
    assert duplicate.status_code == 200
    assert duplicate.json() == {**body, "duplicate": True}
    assert migrated_database.execute("SELECT status FROM invoices").fetchone() == ("PROCESSING",)
    assert migrated_database.execute(
        "SELECT status FROM runs WHERE id=%s", (body["run_id"],)
    ).fetchone() == ("RUNNING",)
    assert migrated_database.execute(
        "SELECT run_id,graph_version FROM ledger WHERE event_type='ingest.duplicate_rejected'"
    ).fetchone() == (UUID(body["run_id"]), "ingestion-old-v1")


async def test_missing_original_response_fails_closed(
    upload_settings: ApiSettings,
    migrated_database: psycopg.Connection[tuple[object, ...]],
) -> None:
    async with client_for(create_app(upload_settings)) as client:
        assert (
            await client.post(
                "/v1/invoices", headers=HEADERS, files={"file": ("a.pdf", PDF, "application/pdf")}
            )
        ).status_code == 201
        migrated_database.execute("DELETE FROM ingestion_requests")
        duplicate = await client.post(
            "/v1/invoices",
            headers={**HEADERS, "Idempotency-Key": "missing-origin"},
            files={"file": ("a.pdf", PDF, "application/pdf")},
        )
    assert_problem(duplicate, 503)
    assert row_counts(migrated_database) == (1, 1, 1, 0)

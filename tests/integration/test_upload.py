"""Real restricted-role transactions and MinIO objects through the async HTTP boundary."""

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import psycopg
import pytest
from minio import Minio
from pydantic import HttpUrl, SecretStr
from sqlalchemy.engine import make_url
from tests.integration.conftest import TEST_PASSWORD, TEST_USER
from tests.unit.test_api import assert_problem, client_for
from tests.unit.test_upload import HEADERS, PDF, TOKEN

from invoiceops_agent.api.app import create_app
from invoiceops_agent.api.settings import ApiSettings
from invoiceops_agent.db.runtime_role import provision_runtime_login
from invoiceops_agent.db.settings import ProvisioningSettings
from invoiceops_agent.graph.ingestion import INGESTION_VERSION, IngestionService, UploadService
from invoiceops_agent.ledger.connection import LedgerConnection
from invoiceops_agent.ledger.errors import LedgerStorageError
from invoiceops_agent.ledger.schemas import AppendEvent, LedgerEvent
from invoiceops_agent.ledger.settings import LedgerSettings
from invoiceops_agent.ledger.writer import LedgerWriter
from invoiceops_agent.tools.ingestion_repository import IngestionRepository
from invoiceops_agent.tools.ingestion_schemas import IngestionResult, RawDocument
from invoiceops_agent.tools.raw_storage import s3_storage
from invoiceops_agent.tools.storage_bootstrap import provision_bucket

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
FIXED_TIME = datetime(2026, 1, 2, tzinfo=UTC)


@pytest.fixture
def upload_settings(
    migrated_database: psycopg.Connection[tuple[object, ...]],
    migration_dsn: str,
    minio_endpoint: str,
    minio_client: Minio,
) -> ApiSettings:
    provision_runtime_login(
        ProvisioningSettings(
            migration_dsn=SecretStr(migration_dsn), app_password=SecretStr(TEST_PASSWORD)
        )
    )
    dsn = (
        make_url(migration_dsn)
        .set(drivername="postgresql", username="invoiceops_app", password=TEST_PASSWORD)
        .render_as_string(hide_password=False)
    )
    minio_client.make_bucket("invoiceops-raw")
    return ApiSettings(
        postgres_dsn=SecretStr(dsn),
        minio_url=HttpUrl(minio_endpoint),
        minio_access_key=SecretStr(TEST_USER),
        minio_secret_key=SecretStr(TEST_PASSWORD),
        service_token=SecretStr(TOKEN),
    )


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


async def test_replay_conflict_and_interim_duplicate_leave_one_atomic_result(
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
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert_problem(conflict, 409)
    assert_problem(duplicate, 409)
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
    assert row_counts(migrated_database) == (1, 1, 1, 1)


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
        result: IngestionResult,
        created_at: datetime,
    ) -> None:
        await IngestionRepository.remember(connection, key, document, result, created_at)
        raise psycopg.OperationalError("synthetic-private-failure")


@pytest.mark.parametrize("failure", ["ledger", "replay"])
async def test_failure_rolls_back_all_rows_without_deleting_shared_raw(
    upload_settings: ApiSettings,
    migrated_database: psycopg.Connection[tuple[object, ...]],
    minio_client: Minio,
    failure: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
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

    async with client_for(create_app(upload_settings, upload_factory=factory)) as client:
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

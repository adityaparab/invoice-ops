"""Isolated infrastructure fixtures with bounded readiness and client timeouts."""

from collections.abc import Iterator

import psycopg
import pytest
from minio import Minio
from pydantic import HttpUrl, SecretStr
from sqlalchemy.engine import URL, make_url
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import ExecWaitStrategy, HttpWaitStrategy
from tests.integration.support import migrate
from urllib3 import PoolManager, Timeout

from invoiceops_agent.api.settings import ApiSettings
from invoiceops_agent.db.runtime_role import provision_runtime_login
from invoiceops_agent.db.settings import ProvisioningSettings

POSTGRES_IMAGE = (
    "pgvector/pgvector:0.8.6-pg16"
    "@sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b"
)
MINIO_IMAGE = "invoiceops-minio:RELEASE.2025-09-07T16-13-09Z"
# These credentials exist only in disposable containers; no application secrets are used.
TEST_USER = "invoiceops_test"
TEST_PASSWORD = "synthetic-test-password"


@pytest.fixture
def postgres_connection() -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    container = (
        DockerContainer(POSTGRES_IMAGE, docker_client_kw={"timeout": 30})
        .with_env("POSTGRES_USER", TEST_USER)
        .with_env("POSTGRES_PASSWORD", TEST_PASSWORD)
        .with_env("POSTGRES_DB", "invoiceops_test")
        .with_exposed_ports(5432)
        .waiting_for(
            ExecWaitStrategy(
                ["pg_isready", "-h", "127.0.0.1", "-U", TEST_USER]
            ).with_startup_timeout(60)
        )
    )
    with (
        container,
        psycopg.connect(
            host=container.get_container_host_ip(),
            port=container.get_exposed_port(5432),
            user=TEST_USER,
            password=TEST_PASSWORD,
            dbname="invoiceops_test",
            connect_timeout=5,
            options="-c statement_timeout=5000",
        ) as connection,
    ):
        yield connection


@pytest.fixture
def minio_endpoint() -> Iterator[str]:
    container = (
        DockerContainer(MINIO_IMAGE, docker_client_kw={"timeout": 30})
        .with_env("MINIO_ROOT_USER", TEST_USER)
        .with_env("MINIO_ROOT_PASSWORD", TEST_PASSWORD)
        .with_command(["server", "/data"])
        .with_exposed_ports(9000)
        .waiting_for(HttpWaitStrategy(9000, "/minio/health/ready").with_startup_timeout(60))
    )
    with container:
        yield f"http://{container.get_container_host_ip()}:{container.get_exposed_port(9000)}"


@pytest.fixture
def minio_client(minio_endpoint: str) -> Iterator[Minio]:
    with PoolManager(timeout=Timeout(connect=5, read=5), retries=False) as http:
        yield Minio(
            minio_endpoint.removeprefix("http://"),
            access_key=TEST_USER,
            secret_key=TEST_PASSWORD,
            secure=False,
            region="us-east-1",
            http_client=http,
        )


@pytest.fixture
def migration_dsn(
    postgres_connection: psycopg.Connection[tuple[object, ...]], monkeypatch: pytest.MonkeyPatch
) -> str:
    info = postgres_connection.info
    dsn = URL.create(
        "postgresql+psycopg",
        username=info.user,
        password=info.password,
        host=info.host,
        port=info.port,
        database=info.dbname,
    ).render_as_string(hide_password=False)
    monkeypatch.setenv("INVOICEOPS_MIGRATION_DSN", dsn)
    postgres_connection.autocommit = True
    return dsn


@pytest.fixture
def migrated_database(
    postgres_connection: psycopg.Connection[tuple[object, ...]], migration_dsn: str
) -> psycopg.Connection[tuple[object, ...]]:
    migrate("upgrade", "head")
    return postgres_connection


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
        service_token=SecretStr("synthetic-upload-token"),
    )

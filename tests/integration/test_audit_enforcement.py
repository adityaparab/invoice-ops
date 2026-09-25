"""Prove audit immutability and runtime credential boundaries in real PostgreSQL."""

import logging
import subprocess
import sys
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql
from pydantic import SecretStr
from sqlalchemy.engine import make_url
from tests.integration.support import (
    EXCEPTION_ID,
    INVOICE_ID,
    ROOT,
    RUN_ID,
    insert_decision,
    insert_ledger,
    migrate,
    run_alembic,
    seed_invoice_and_run,
)

from invoiceops_agent.db.runtime_role import (
    RuntimeRoleError,
    provision_runtime_login,
    validate_runtime_role,
)
from invoiceops_agent.db.settings import ProvisioningSettings

pytestmark = pytest.mark.integration
APP_PASSWORD = "synthetic-runtime-password:@/unicode-\u00e9"


def _settings(dsn: str, password: str = APP_PASSWORD) -> ProvisioningSettings:
    return ProvisioningSettings(migration_dsn=SecretStr(dsn), app_password=SecretStr(password))


def _runtime_dsn(dsn: str, password: str = APP_PASSWORD) -> str:
    return (
        make_url(dsn)
        .set(drivername="postgresql", username="invoiceops_app", password=password)
        .render_as_string(hide_password=False)
    )


def _seed_audit_records(connection: psycopg.Connection[tuple[object, ...]]) -> None:
    seed_invoice_and_run(connection)
    insert_ledger(connection)
    connection.execute(
        "INSERT INTO exceptions (id, run_id, invoice_id, exception_type, priority, sla_due_at, "
        "evidence, recommendation) VALUES (%s, %s, %s, 'PRICE_MISMATCH', 1, "
        "'2026-09-24T08:00:00Z', '{}', '{}')",
        (EXCEPTION_ID, RUN_ID, INVOICE_ID),
    )
    insert_decision(connection)


@pytest.fixture
def runtime_connection(
    migrated_database: psycopg.Connection[tuple[object, ...]], migration_dsn: str
) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    provision_runtime_login(_settings(migration_dsn))
    with psycopg.connect(
        _runtime_dsn(migration_dsn),
        connect_timeout=5,
        autocommit=True,
        options="-c statement_timeout=5000",
    ) as connection:
        yield connection


def test_owner_cannot_update_delete_or_truncate_audit_even_for_empty_matches(
    migrated_database: psycopg.Connection[tuple[object, ...]],
) -> None:
    _seed_audit_records(migrated_database)
    for table in ("ledger", "decisions"):
        for statement in (
            f"UPDATE {table} SET actor_id = 'changed'",
            f"UPDATE {table} SET actor_id = 'changed' WHERE false",
            f"DELETE FROM {table}",
            f"DELETE FROM {table} WHERE false",
            f"TRUNCATE {table}",
        ):
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="append-only"):
                migrated_database.execute(statement)
        assert migrated_database.execute(f"SELECT count(*) FROM {table}").fetchone() == (1,)
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="append-only"):
        migrated_database.execute("TRUNCATE invoices CASCADE")


def test_owner_replication_mode_does_not_bypass_always_enabled_triggers(
    migrated_database: psycopg.Connection[tuple[object, ...]],
) -> None:
    for table in ("ledger", "decisions"):
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="append-only"):
            with migrated_database.transaction():
                migrated_database.execute("SET LOCAL session_replication_role = replica")
                migrated_database.execute(f"DELETE FROM {table} WHERE false")


def test_runtime_can_append_read_and_do_operational_crud_but_cannot_mutate_audit(
    runtime_connection: psycopg.Connection[tuple[object, ...]],
    migrated_database: psycopg.Connection[tuple[object, ...]],
) -> None:
    assert runtime_connection.execute("SELECT current_user").fetchone() == ("invoiceops_app",)
    _seed_audit_records(runtime_connection)
    for table in ("ledger", "decisions"):
        assert runtime_connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (1,)
        for statement in (
            f"UPDATE {table} SET actor_id = 'changed'",
            f"DELETE FROM {table}",
            f"TRUNCATE {table}",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                runtime_connection.execute(statement)
    runtime_connection.execute(
        "INSERT INTO vendors (external_id, name) VALUES ('runtime-fixture', 'Synthetic Vendor')"
    )
    runtime_connection.execute("UPDATE vendors SET status = 'INACTIVE'")
    assert runtime_connection.execute("SELECT status FROM vendors").fetchone() == ("INACTIVE",)
    runtime_connection.execute("DELETE FROM vendors")
    assert runtime_connection.execute("SELECT count(*) FROM vendors").fetchone() == (0,)
    validate_runtime_role(migrated_database)
    for statement in (
        "TRUNCATE vendors",
        "CREATE TABLE public.forbidden (id integer)",
        "ALTER TABLE ledger DISABLE TRIGGER ledger_append_only",
        "SET session_replication_role = replica",
        "SELECT * FROM alembic_version",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            runtime_connection.execute(statement)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        runtime_connection.execute(
            sql.SQL("SET ROLE {}").format(sql.Identifier(migrated_database.info.user))
        )


def test_role_starts_without_login_and_provisioning_uses_scram_without_logging_secrets(
    migrated_database: psycopg.Connection[tuple[object, ...]],
    migration_dsn: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert migrated_database.execute(
        "SELECT rolcanlogin, rolpassword FROM pg_authid WHERE rolname = 'invoiceops_app'"
    ).fetchone() == (False, None)
    with caplog.at_level(logging.INFO):
        provision_runtime_login(_settings(migration_dsn))
    row = migrated_database.execute(
        "SELECT rolcanlogin, rolpassword FROM pg_authid WHERE rolname = 'invoiceops_app'"
    ).fetchone()
    assert row is not None and row[0] is True
    assert isinstance(row[1], str) and row[1].startswith("SCRAM-SHA-256$")
    assert APP_PASSWORD not in row[1]
    assert APP_PASSWORD not in caplog.text
    assert row[1] not in caplog.text
    assert "event=database.runtime_role.provisioned" in caplog.text
    rotated_password = "replacement-synthetic-runtime-password"
    provision_runtime_login(_settings(migration_dsn, rotated_password))
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(_runtime_dsn(migration_dsn), connect_timeout=5).close()
    with psycopg.connect(
        _runtime_dsn(migration_dsn, rotated_password), connect_timeout=5
    ) as rotated:
        assert rotated.execute("SELECT current_user").fetchone() == ("invoiceops_app",)


@pytest.mark.parametrize(
    "unsafe",
    [
        "unmanaged",
        "privileged",
        "member",
        "reverse_member",
        "owner",
        "shared",
        "shared_database",
        "parameter_grant",
        "database_create",
    ],
)
def test_upgrade_refuses_preexisting_unsafe_roles_without_altering_credentials(
    postgres_connection: psycopg.Connection[tuple[object, ...]], migration_dsn: str, unsafe: str
) -> None:
    migrate("upgrade", "0001_initial_schema")
    postgres_connection.execute(
        "CREATE ROLE invoiceops_app LOGIN NOINHERIT PASSWORD 'unchanged-synthetic-password'"
    )
    before = postgres_connection.execute(
        "SELECT rolpassword FROM pg_authid WHERE rolname = 'invoiceops_app'"
    ).fetchone()
    if unsafe != "unmanaged":
        postgres_connection.execute(
            sql.SQL("COMMENT ON ROLE invoiceops_app IS {}").format(
                sql.Literal("invoiceops-managed-runtime-v1:" + postgres_connection.info.dbname)
            )
        )
    if unsafe == "privileged":
        postgres_connection.execute("ALTER ROLE invoiceops_app CREATEROLE")
    elif unsafe == "member":
        postgres_connection.execute("CREATE ROLE unrelated_group")
        postgres_connection.execute("GRANT unrelated_group TO invoiceops_app")
    elif unsafe == "reverse_member":
        postgres_connection.execute("CREATE ROLE unrelated_login LOGIN")
        postgres_connection.execute("GRANT invoiceops_app TO unrelated_login")
    elif unsafe == "owner":
        postgres_connection.execute("CREATE TABLE preexisting_table (id integer)")
        postgres_connection.execute("ALTER TABLE preexisting_table OWNER TO invoiceops_app")
    elif unsafe == "shared":
        postgres_connection.execute("CREATE DATABASE unrelated_database")
        other_dsn = (
            make_url(migration_dsn)
            .set(drivername="postgresql", database="unrelated_database")
            .render_as_string(hide_password=False)
        )
        with psycopg.connect(other_dsn, connect_timeout=5) as other:
            other.execute("CREATE TABLE unrelated_table (id integer)")
            other.execute("GRANT SELECT ON unrelated_table TO invoiceops_app")
    elif unsafe == "shared_database":
        postgres_connection.execute("CREATE DATABASE unrelated_database")
        postgres_connection.execute(
            "GRANT CONNECT ON DATABASE unrelated_database TO invoiceops_app"
        )
    elif unsafe == "parameter_grant":
        postgres_connection.execute(
            "GRANT SET ON PARAMETER session_replication_role TO invoiceops_app"
        )
    elif unsafe == "database_create":
        postgres_connection.execute(
            sql.SQL("GRANT CREATE ON DATABASE {} TO invoiceops_app").format(
                sql.Identifier(postgres_connection.info.dbname)
            )
        )
    result = run_alembic("upgrade", "head")
    assert result.returncode != 0
    assert "runtime role" in result.stderr
    assert (
        postgres_connection.execute(
            "SELECT rolpassword FROM pg_authid WHERE rolname = 'invoiceops_app'"
        ).fetchone()
        == before
    )
    assert postgres_connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
        "0001_initial_schema",
    )
    assert "unchanged-synthetic-password" not in result.stderr


@pytest.mark.parametrize(
    "corruption",
    [
        "ALTER ROLE invoiceops_app CREATEROLE",
        "GRANT UPDATE (actor_id) ON ledger TO invoiceops_app",
        "ALTER TABLE decisions DISABLE TRIGGER decisions_append_only",
        "GRANT TRUNCATE ON vendors TO invoiceops_app",
        "GRANT SELECT ON ledger TO invoiceops_app WITH GRANT OPTION",
        "GRANT SELECT (actor_id) ON decisions TO invoiceops_app WITH GRANT OPTION",
        "GRANT USAGE ON SCHEMA public TO invoiceops_app WITH GRANT OPTION",
    ],
)
def test_provisioning_refuses_unsafe_role_state_before_setting_a_password(
    migrated_database: psycopg.Connection[tuple[object, ...]], migration_dsn: str, corruption: str
) -> None:
    migrated_database.execute(corruption)
    with pytest.raises(RuntimeRoleError):
        provision_runtime_login(_settings(migration_dsn))
    assert migrated_database.execute(
        "SELECT rolcanlogin, rolpassword FROM pg_authid WHERE rolname = 'invoiceops_app'"
    ).fetchone() == (False, None)


def test_downgrade_preserves_role_and_password_then_reupgrade_restores_boundaries(
    runtime_connection: psycopg.Connection[tuple[object, ...]],
    migrated_database: psycopg.Connection[tuple[object, ...]],
    migration_dsn: str,
) -> None:
    before = migrated_database.execute(
        "SELECT oid, rolpassword FROM pg_authid WHERE rolname = 'invoiceops_app'"
    ).fetchone()
    migrate("downgrade", "0001_initial_schema")
    for table in ("ledger", "decisions"):
        migrated_database.execute(f"DELETE FROM {table} WHERE false")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            runtime_connection.execute(f"SELECT * FROM {table}")
    assert (
        migrated_database.execute(
            "SELECT oid, rolpassword FROM pg_authid WHERE rolname = 'invoiceops_app'"
        ).fetchone()
        == before
    )
    migrate("upgrade", "head")
    validate_runtime_role(migrated_database)
    assert (
        migrated_database.execute(
            "SELECT oid, rolpassword FROM pg_authid WHERE rolname = 'invoiceops_app'"
        ).fetchone()
        == before
    )
    with psycopg.connect(_runtime_dsn(migration_dsn), connect_timeout=5) as runtime:
        assert runtime.execute("SELECT count(*) FROM ledger").fetchone() == (0,)
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        migrated_database.execute("DELETE FROM ledger WHERE false")


def test_bootstrap_cli_runs_migrations_then_login_and_redacts_configuration_failures(
    migration_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INVOICEOPS_APP_PASSWORD", APP_PASSWORD)
    for role in ("ANALYST", "MANAGER", "AUDITOR", "PLATFORM"):
        monkeypatch.setenv(f"INVOICEOPS_{role}_EMAIL", f"{role.lower()}@example.test")
        monkeypatch.setenv(f"INVOICEOPS_{role}_PASSWORD", f"synthetic-{role.lower()}-password")
    result = subprocess.run(
        [sys.executable, "-m", "invoiceops_agent.db.migrate"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "event=database.bootstrap.completed" in result.stderr
    assert APP_PASSWORD not in result.stderr
    with psycopg.connect(_runtime_dsn(migration_dsn), connect_timeout=5) as runtime:
        assert runtime.execute("SELECT count(*) FROM ledger").fetchone() == (0,)
    monkeypatch.setenv("INVOICEOPS_MIGRATION_DSN", "invalid-synthetic-owner-secret")
    failed = subprocess.run(
        [sys.executable, "-m", "invoiceops_agent.db.migrate"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert failed.returncode == 1
    assert "event=database.bootstrap.failed error_type=ValidationError" in failed.stderr
    assert "invalid-synthetic-owner-secret" not in failed.stderr
    assert APP_PASSWORD not in failed.stderr

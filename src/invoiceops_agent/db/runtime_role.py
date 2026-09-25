"""Provision the managed runtime login without sending plaintext passwords in SQL."""

import logging
from time import perf_counter

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from invoiceops_agent.db.settings import (
    OWNER_CONNECT_TIMEOUT,
    OWNER_CONNECTION_OPTIONS,
    ProvisioningSettings,
)

logger = logging.getLogger(__name__)
RUNTIME_ROLE = "invoiceops_app"
AUDIT_TABLES = ("ledger", "decisions")
OPERATIONAL_TABLES = (
    "vendors",
    "purchase_orders",
    "goods_receipts",
    "invoices",
    "invoice_lines",
    "runs",
    "checkpoints",
    "ingestion_requests",
    "webhook_nonces",
    "exceptions",
)


class RuntimeRoleError(Exception):
    """The database role or grants cannot safely be used for the application."""


def validate_runtime_role(connection: psycopg.Connection[tuple[object, ...]]) -> None:
    """Fail closed if an existing role belongs elsewhere or can bypass runtime boundaries."""
    role = connection.execute(
        """
        SELECT shobj_description(r.oid, 'pg_authid') =
                   'invoiceops-managed-runtime-v1:' || current_database()
           AND NOT (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolinherit
                    OR r.rolreplication OR r.rolbypassrls)
           AND NOT EXISTS (
               SELECT FROM pg_auth_members
               WHERE member = r.oid OR (roleid = r.oid AND NOT (
                   member = (SELECT oid FROM pg_roles WHERE rolname = current_user)
                   AND admin_option AND NOT inherit_option AND NOT set_option
               ))
           )
           AND NOT EXISTS (
               SELECT FROM pg_shdepend
               WHERE refclassid = 'pg_authid'::regclass AND refobjid = r.oid
                 AND (deptype = 'o' OR dbid NOT IN (
                          0, (SELECT oid FROM pg_database WHERE datname = current_database())
                      ) OR (dbid = 0 AND deptype = 'a' AND NOT (
                          classid = 'pg_database'::regclass AND objid = (
                              SELECT oid FROM pg_database WHERE datname = current_database()
                          )
                      )))
           )
        FROM pg_roles r WHERE rolname = %s
        """,
        (RUNTIME_ROLE,),
    ).fetchone()
    if role != (True,):
        raise RuntimeRoleError("Runtime role is missing, unsafe, or managed by another database")
    schema = connection.execute(
        "SELECT has_schema_privilege(%s, 'public', 'USAGE') "
        "AND NOT has_schema_privilege(%s, 'public', 'CREATE,USAGE WITH GRANT OPTION') "
        "AND has_database_privilege(%s, current_database(), 'CONNECT') "
        "AND NOT has_database_privilege(%s, current_database(), "
        "'CREATE,CONNECT WITH GRANT OPTION,TEMP WITH GRANT OPTION')",
        (RUNTIME_ROLE, RUNTIME_ROLE, RUNTIME_ROLE, RUNTIME_ROLE),
    ).fetchone()
    if schema != (True,):
        raise RuntimeRoleError("Runtime schema privileges do not match the restricted contract")
    for table in (*OPERATIONAL_TABLES, *AUDIT_TABLES):
        _validate_table_privileges(connection, table, audit=table in AUDIT_TABLES)
    _validate_auth_privileges(connection)
    triggers = connection.execute(
        "SELECT count(*) FROM pg_trigger t "
        "JOIN pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_proc p ON p.oid = t.tgfoid "
        "WHERE n.nspname = 'public' AND t.tgenabled = 'A' AND NOT t.tgisinternal "
        "AND p.proname = 'reject_audit_mutation' "
        "AND (c.relname, t.tgname) IN "
        "(('ledger', 'ledger_append_only'), ('decisions', 'decisions_append_only'))"
    ).fetchone()
    if triggers != (2,):
        raise RuntimeRoleError(
            "Both append-only audit triggers must be enabled before runtime login"
        )


def _validate_auth_privileges(connection: psycopg.Connection[tuple[object, ...]]) -> None:
    for table, allowed in (
        ("auth_users", ["SELECT"]),
        ("auth_sessions", ["SELECT", "INSERT", "DELETE"]),
    ):
        actual = connection.execute(
            "SELECT bool_and(has_table_privilege(%s, %s, privilege) = (privilege = ANY(%s))) "
            "FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES',"
            "'TRIGGER']) AS privilege",
            (RUNTIME_ROLE, f"public.{table}", allowed),
        ).fetchone()
        if actual != (True,):
            raise RuntimeRoleError(f"Runtime {table} privileges are unsafe")
        grants = connection.execute(
            "SELECT has_table_privilege(%s, %s, "
            "'SELECT WITH GRANT OPTION,INSERT WITH GRANT OPTION,DELETE WITH GRANT OPTION') "
            "OR has_any_column_privilege(%s, %s, 'UPDATE WITH GRANT OPTION')",
            (RUNTIME_ROLE, f"public.{table}", RUNTIME_ROLE, f"public.{table}"),
        ).fetchone()
        if grants != (False,):
            raise RuntimeRoleError(f"Runtime {table} can delegate privileges")
    updates = connection.execute(
        "SELECT bool_and(has_column_privilege(%s, 'public.auth_users', a.attname, 'UPDATE') "
        "= (a.attname = ANY(ARRAY['failed_attempts', 'locked_until']))) "
        "FROM pg_attribute a WHERE a.attrelid = 'public.auth_users'::regclass "
        "AND a.attnum > 0 AND NOT a.attisdropped",
        (RUNTIME_ROLE,),
    ).fetchone()
    if updates != (True,):
        raise RuntimeRoleError("Runtime auth_users column privileges are unsafe")


def _validate_table_privileges(
    connection: psycopg.Connection[tuple[object, ...]], table: str, *, audit: bool
) -> None:
    allowed = ["SELECT", "INSERT"] if audit else ["SELECT", "INSERT", "UPDATE", "DELETE"]
    actual = connection.execute(
        "SELECT bool_and(has_table_privilege(%s, %s, privilege) = (privilege = ANY(%s))) "
        "FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) "
        "AS privilege",
        (RUNTIME_ROLE, f"public.{table}", allowed),
    ).fetchone()
    if actual != (True,):
        raise RuntimeRoleError(f"Runtime {table} privileges do not match the restricted contract")
    grant_options = connection.execute(
        "SELECT has_table_privilege(%s, %s, "
        "'SELECT WITH GRANT OPTION,INSERT WITH GRANT OPTION,UPDATE WITH GRANT OPTION,"
        "DELETE WITH GRANT OPTION') OR has_any_column_privilege(%s, %s, "
        "'SELECT WITH GRANT OPTION,INSERT WITH GRANT OPTION,UPDATE WITH GRANT OPTION,"
        "REFERENCES WITH GRANT OPTION')",
        (RUNTIME_ROLE, f"public.{table}", RUNTIME_ROLE, f"public.{table}"),
    ).fetchone()
    if grant_options != (False,):
        raise RuntimeRoleError(
            "Runtime role must not be able to delegate table or column privileges"
        )
    if audit:
        columns = connection.execute(
            "SELECT has_any_column_privilege(%s, %s, 'UPDATE,REFERENCES')",
            (RUNTIME_ROLE, f"public.{table}"),
        ).fetchone()
        if columns != (False,):
            raise RuntimeRoleError("Runtime role has forbidden audit column privileges")


def provision_runtime_login(settings: ProvisioningSettings) -> None:
    """Rotate our isolated runtime login and verify a password-authenticated connection."""
    started = perf_counter()
    owner_url = make_url(settings.migration_dsn.get_secret_value()).set(drivername="postgresql")
    with psycopg.connect(
        owner_url.render_as_string(hide_password=False),
        connect_timeout=OWNER_CONNECT_TIMEOUT,
        options=OWNER_CONNECTION_OPTIONS,
    ) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(761923, 7)")
        validate_runtime_role(connection)
        verifier = connection.pgconn.encrypt_password(
            settings.app_password.get_secret_value().encode("utf-8"),
            RUNTIME_ROLE.encode("ascii"),
            b"scram-sha-256",
        ).decode("ascii")
        connection.execute(
            sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(RUNTIME_ROLE), sql.Literal(verifier)
            )
        )
    runtime_url = owner_url.set(
        username=RUNTIME_ROLE, password=settings.app_password.get_secret_value()
    )
    with psycopg.connect(
        runtime_url.render_as_string(hide_password=False),
        connect_timeout=OWNER_CONNECT_TIMEOUT,
        options="-c timezone=UTC -c search_path=public -c statement_timeout=5000",
    ) as runtime_connection:
        if runtime_connection.execute("SELECT current_user").fetchone() != (RUNTIME_ROLE,):
            raise RuntimeRoleError("Runtime login verification returned an unexpected identity")
    logger.info(
        "event=database.runtime_role.provisioned role=%s duration_ms=%.1f",
        RUNTIME_ROLE,
        (perf_counter() - started) * 1000,
    )

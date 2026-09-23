"""Enforce immutable audit history and grant restricted runtime access.

Revision ID: 0002_append_only_audit
Revises: 0001_initial_schema
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_append_only_audit"
down_revision: str | Sequence[str] | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AUDIT_TABLES = ("ledger", "decisions")
_OPERATIONAL_TABLES = (
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


def _prepare_runtime_role() -> None:
    # The marker binds this cluster-wide identity to one database; never adopt another role.
    op.execute(
        """
        DO $role$
        DECLARE
            runtime pg_roles%ROWTYPE;
            database_oid oid := (SELECT oid FROM pg_database WHERE datname = current_database());
            marker text := 'invoiceops-managed-runtime-v1:' || current_database();
        BEGIN
            PERFORM pg_advisory_xact_lock(761923, 7);
            SELECT * INTO runtime FROM pg_roles WHERE rolname = 'invoiceops_app';
            IF NOT FOUND THEN
                CREATE ROLE invoiceops_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                    NOINHERIT NOREPLICATION NOBYPASSRLS;
                EXECUTE format('COMMENT ON ROLE invoiceops_app IS %L', marker);
                SELECT * INTO runtime FROM pg_roles WHERE rolname = 'invoiceops_app';
            END IF;
            IF shobj_description(runtime.oid, 'pg_authid') IS DISTINCT FROM marker
                OR runtime.rolsuper OR runtime.rolcreatedb OR runtime.rolcreaterole
                OR runtime.rolinherit OR runtime.rolreplication OR runtime.rolbypassrls
                OR EXISTS (
                    SELECT FROM pg_auth_members
                    WHERE member = runtime.oid
                       OR (roleid = runtime.oid AND NOT (
                           member = (SELECT oid FROM pg_roles WHERE rolname = current_user)
                           AND admin_option AND NOT inherit_option AND NOT set_option
                       ))
                )
                OR EXISTS (
                    SELECT FROM pg_shdepend
                    WHERE refclassid = 'pg_authid'::regclass AND refobjid = runtime.oid
                      AND (deptype = 'o' OR dbid NOT IN (0, database_oid)
                           OR (dbid = 0 AND deptype = 'a' AND NOT (
                               classid = 'pg_database'::regclass AND objid = database_oid
                           )))
                )
            THEN
                RAISE EXCEPTION 'invoiceops_app is not an isolated managed runtime role'
                    USING ERRCODE = '42501';
            END IF;
        END
        $role$;
        """
    )


def _grant_runtime_access() -> None:
    op.execute(
        "DO $connect$ BEGIN EXECUTE format('GRANT CONNECT ON DATABASE %I TO invoiceops_app', "
        "current_database()); END $connect$"
    )
    op.execute("GRANT USAGE ON SCHEMA public TO invoiceops_app")
    for table in (*_OPERATIONAL_TABLES, *_AUDIT_TABLES):
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE public.{table} FROM invoiceops_app")
        privileges = (
            "SELECT, INSERT" if table in _AUDIT_TABLES else "SELECT, INSERT, UPDATE, DELETE"
        )
        op.execute(f"GRANT {privileges} ON TABLE public.{table} TO invoiceops_app")
    op.execute(
        """
        DO $permissions$
        DECLARE
            table_name text;
        BEGIN
            IF has_schema_privilege('invoiceops_app', 'public', 'CREATE,USAGE WITH GRANT OPTION')
               OR has_database_privilege('invoiceops_app', current_database(),
                                         'CREATE,CONNECT WITH GRANT OPTION,TEMP WITH GRANT OPTION')
            THEN
                RAISE EXCEPTION 'runtime role has excessive database or schema privileges'
                    USING ERRCODE = '42501';
            END IF;
            FOREACH table_name IN ARRAY ARRAY['ledger', 'decisions'] LOOP
                IF has_table_privilege('invoiceops_app', 'public.' || table_name,
                                      'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                   OR has_any_column_privilege('invoiceops_app', 'public.' || table_name,
                                               'UPDATE,REFERENCES') THEN
                    RAISE EXCEPTION 'runtime role has forbidden audit privileges'
                        USING ERRCODE = '42501';
                END IF;
            END LOOP;
            FOREACH table_name IN ARRAY ARRAY[
                'vendors', 'purchase_orders', 'goods_receipts', 'invoices', 'invoice_lines',
                'runs', 'checkpoints', 'ingestion_requests', 'webhook_nonces', 'exceptions',
                'ledger', 'decisions'
            ] LOOP
                IF has_table_privilege('invoiceops_app', 'public.' || table_name,
                    'TRUNCATE,REFERENCES,TRIGGER,SELECT WITH GRANT OPTION,INSERT WITH GRANT OPTION,'
                    'UPDATE WITH GRANT OPTION,DELETE WITH GRANT OPTION')
                   OR has_any_column_privilege('invoiceops_app', 'public.' || table_name,
                    'SELECT WITH GRANT OPTION,INSERT WITH GRANT OPTION,'
                    'UPDATE WITH GRANT OPTION,REFERENCES WITH GRANT OPTION') THEN
                    RAISE EXCEPTION 'runtime role has forbidden privileges or grant options'
                        USING ERRCODE = '42501';
                END IF;
            END LOOP;
        END
        $permissions$;
        """
    )


def upgrade() -> None:
    op.execute("SET LOCAL search_path = public")
    _prepare_runtime_role()
    op.execute(
        """
        CREATE FUNCTION public.reject_audit_mutation() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        BEGIN
            RAISE EXCEPTION '% is append-only; append a superseding entry instead', TG_TABLE_NAME
                USING ERRCODE = '55000';
        END
        $function$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.reject_audit_mutation() FROM PUBLIC")
    for table in _AUDIT_TABLES:
        trigger = f"{table}_append_only"
        op.execute(
            f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE OR TRUNCATE ON public.{table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION public.reject_audit_mutation()"
        )
        op.execute(f"ALTER TABLE public.{table} ENABLE ALWAYS TRIGGER {trigger}")
    _grant_runtime_access()


def downgrade() -> None:
    op.execute("SET LOCAL search_path = public")
    for table in (*_OPERATIONAL_TABLES, *_AUDIT_TABLES):
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE public.{table} FROM invoiceops_app")
    op.execute("REVOKE USAGE ON SCHEMA public FROM invoiceops_app")
    op.execute(
        "DO $connect$ BEGIN EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM invoiceops_app', "
        "current_database()); END $connect$"
    )
    for table in _AUDIT_TABLES:
        op.execute(f"DROP TRIGGER {table}_append_only ON public.{table}")
    op.execute("DROP FUNCTION public.reject_audit_mutation()")
    # A database rollback must not delete a cluster-wide login or rotate its credentials.

"""Shared real-schema integration helpers and synthetic audit records."""

import subprocess
import sys
from pathlib import Path
from uuid import UUID

import psycopg

ROOT = Path(__file__).resolve().parents[2]
INVOICE_ID = UUID("00000000-0000-4000-8000-000000000001")
RUN_ID = UUID("00000000-0000-4000-8000-000000000002")
EXCEPTION_ID = UUID("00000000-0000-4000-8000-000000000003")


def run_alembic(direction: str, target: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini"), direction, target],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )


def migrate(direction: str, target: str) -> None:
    result = run_alembic(direction, target)
    assert result.returncode == 0, result.stderr


def seed_invoice_and_run(connection: psycopg.Connection[tuple[object, ...]]) -> None:
    connection.execute(
        "INSERT INTO invoices (id, content_hash, raw_ref, content_type, source) "
        "VALUES (%s, %s, %s, 'application/pdf', 'UPLOAD')",
        (INVOICE_ID, "a" * 64, "sha256/aa/" + "a" * 64),
    )
    connection.execute(
        "INSERT INTO runs (id, invoice_id, graph_version, trace_id) VALUES (%s, %s, %s, %s)",
        (RUN_ID, INVOICE_ID, "graph@v1", "0" * 32),
    )


def insert_ledger(
    connection: psycopg.Connection[tuple[object, ...]],
    *,
    sequence: int = 1,
    model_version: str | None = "model@v1",
    actor_type: str = "SYSTEM",
    invoice_id: UUID = INVOICE_ID,
) -> None:
    connection.execute(
        "INSERT INTO ledger (run_id, invoice_id, sequence, event_type, actor_type, actor_id, "
        "graph_version, model_version, prompt_version, policy_version, payload) "
        "VALUES (%s, %s, %s, 'ingest.accepted', %s, 'synthetic-service', "
        "'graph@v1', %s, 'prompt@v1', 'policy@v1', '{}')",
        (RUN_ID, invoice_id, sequence, actor_type, model_version),
    )


def insert_decision(
    connection: psycopg.Connection[tuple[object, ...]],
    *,
    key: str = "synthetic-decision-1",
    action: str = "APPROVE",
    policy_version: str | None = "policy@v1",
) -> None:
    connection.execute(
        "INSERT INTO decisions (run_id, invoice_id, exception_id, idempotency_key, action, "
        "rationale, reason_code, actor_type, actor_id, graph_version, model_version, "
        "prompt_version, policy_version) "
        "VALUES (%s, %s, %s, %s, %s, 'Checked synthetic evidence', 'MATCH_CONFIRMED', "
        "'HUMAN', 'synthetic-reviewer', 'graph@v1', 'model@v1', 'prompt@v1', %s)",
        (RUN_ID, INVOICE_ID, EXCEPTION_ID, key, action, policy_version),
    )

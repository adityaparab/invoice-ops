"""Signed email ingress, nonce durability, and replay against real storage and Postgres."""

import asyncio
import hashlib
from datetime import UTC, datetime

import psycopg
import pytest
from minio import Minio
from pydantic import SecretStr
from tests.integration.test_upload import failure_factory, row_counts
from tests.unit.test_api import assert_problem, client_for
from tests.unit.test_upload import TOKEN
from tests.unit.test_webhook import PDF as EMAIL_PDF
from tests.unit.test_webhook import SECRET, envelope, signed_headers

from invoiceops_agent.api.app import create_app
from invoiceops_agent.api.settings import ApiSettings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
NOW = datetime.now(UTC).replace(microsecond=0)


def settings_for_email(settings: ApiSettings) -> ApiSettings:
    return settings.model_copy(update={"webhook_secret": SecretStr(SECRET)})


def email_headers(body: bytes, *, nonce: str, key: str) -> dict[str, str]:
    return signed_headers(body, nonce=nonce, timestamp=int(NOW.timestamp()), key=key)


def nonce_count(connection: psycopg.Connection[tuple[object, ...]]) -> int:
    row = connection.execute("SELECT count(*) FROM webhook_nonces").fetchone()
    assert row is not None
    assert isinstance(row[0], int)
    return row[0]


async def test_email_acceptance_replay_duplicate_and_cross_source_key_conflict(
    upload_settings: ApiSettings,
    migrated_database: psycopg.Connection[tuple[object, ...]],
    minio_client: Minio,
) -> None:
    raw = envelope()
    app = create_app(settings_for_email(upload_settings), webhook_clock=lambda: NOW)
    async with client_for(app) as client:
        accepted = await client.post(
            "/v1/invoices/email-webhook",
            headers=email_headers(raw, nonce="synthetic-email-nonce-0001", key="email-key-1"),
            content=raw,
        )
        replay = await client.post(
            "/v1/invoices/email-webhook",
            headers=email_headers(raw, nonce="synthetic-email-nonce-0002", key="email-key-1"),
            content=raw,
        )
        reused = await client.post(
            "/v1/invoices/email-webhook",
            headers=email_headers(raw, nonce="synthetic-email-nonce-0001", key="email-key-2"),
            content=raw,
        )
        duplicate = await client.post(
            "/v1/invoices/email-webhook",
            headers=email_headers(raw, nonce="synthetic-email-nonce-0003", key="email-key-2"),
            content=raw,
        )
        cross_source = await client.post(
            "/v1/invoices",
            headers={"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": "email-key-1"},
            files={"file": ("synthetic.pdf", EMAIL_PDF, "application/pdf")},
        )
    assert accepted.status_code == replay.status_code == 201
    assert accepted.json() == replay.json()
    assert_problem(reused, 409)
    assert duplicate.status_code == 200
    assert duplicate.json() == {**accepted.json(), "duplicate": True}
    assert_problem(cross_source, 409)
    assert row_counts(migrated_database) == (1, 1, 2, 2)
    assert nonce_count(migrated_database) == 3
    rows = migrated_database.execute(
        "SELECT event_type, payload FROM ledger ORDER BY sequence"
    ).fetchall()
    assert [row[0] for row in rows] == ["ingest.accepted", "ingest.duplicate_rejected"]
    assert all(isinstance(row[1], dict) and row[1]["source"] == "EMAIL" for row in rows)
    content_hash = hashlib.sha256(EMAIL_PDF).hexdigest()
    stored = minio_client.get_object("invoiceops-raw", f"sha256/{content_hash[:2]}/{content_hash}")
    try:
        assert stored.read() == EMAIL_PDF
    finally:
        stored.close()
        stored.release_conn()


async def test_failed_transaction_leaves_nonce_reusable(
    upload_settings: ApiSettings,
    migrated_database: psycopg.Connection[tuple[object, ...]],
) -> None:
    settings = settings_for_email(upload_settings)
    raw = envelope()
    headers = email_headers(raw, nonce="synthetic-email-rollback-0001", key="email-rollback")
    async with client_for(
        create_app(settings, upload_factory=failure_factory("ledger"), webhook_clock=lambda: NOW)
    ) as client:
        failed = await client.post("/v1/invoices/email-webhook", headers=headers, content=raw)
    assert_problem(failed, 503)
    assert row_counts(migrated_database) == (0, 0, 0, 0)
    assert nonce_count(migrated_database) == 0
    async with client_for(create_app(settings, webhook_clock=lambda: NOW)) as client:
        retry = await client.post("/v1/invoices/email-webhook", headers=headers, content=raw)
    assert retry.status_code == 201
    assert nonce_count(migrated_database) == 1


async def test_concurrent_fresh_nonces_same_key_commit_without_duplicate_event(
    upload_settings: ApiSettings,
    migrated_database: psycopg.Connection[tuple[object, ...]],
) -> None:
    raw = envelope()
    app = create_app(settings_for_email(upload_settings), webhook_clock=lambda: NOW)
    async with client_for(app) as client:
        first, second = await asyncio.gather(
            client.post(
                "/v1/invoices/email-webhook",
                headers=email_headers(raw, nonce="synthetic-email-race-0001", key="email-race"),
                content=raw,
            ),
            client.post(
                "/v1/invoices/email-webhook",
                headers=email_headers(raw, nonce="synthetic-email-race-0002", key="email-race"),
                content=raw,
            ),
        )
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert row_counts(migrated_database) == (1, 1, 1, 1)
    assert nonce_count(migrated_database) == 2


async def test_conflicting_key_does_not_claim_nonce_and_same_nonce_race_has_one_winner(
    upload_settings: ApiSettings,
    migrated_database: psycopg.Connection[tuple[object, ...]],
) -> None:
    raw = envelope()
    changed = envelope(EMAIL_PDF + b" changed")
    app = create_app(settings_for_email(upload_settings), webhook_clock=lambda: NOW)
    async with client_for(app) as client:
        accepted = await client.post(
            "/v1/invoices/email-webhook",
            headers=email_headers(raw, nonce="synthetic-email-conflict-0001", key="email-conflict"),
            content=raw,
        )
        conflict = await client.post(
            "/v1/invoices/email-webhook",
            headers=email_headers(
                changed, nonce="synthetic-email-conflict-0002", key="email-conflict"
            ),
            content=changed,
        )
        first, second = await asyncio.gather(
            client.post(
                "/v1/invoices/email-webhook",
                headers=email_headers(
                    changed, nonce="synthetic-email-conflict-0002", key="email-new-1"
                ),
                content=changed,
            ),
            client.post(
                "/v1/invoices/email-webhook",
                headers=email_headers(
                    changed, nonce="synthetic-email-conflict-0002", key="email-new-2"
                ),
                content=changed,
            ),
        )
    assert accepted.status_code == 201
    assert_problem(conflict, 409)
    assert sorted((first.status_code, second.status_code)) == [201, 409]
    assert nonce_count(migrated_database) == 2
    assert row_counts(migrated_database) == (2, 2, 2, 2)


async def test_invalid_document_and_storage_failure_leave_nonces_reusable(
    upload_settings: ApiSettings,
    migrated_database: psycopg.Connection[tuple[object, ...]],
) -> None:
    settings = settings_for_email(upload_settings)
    raw = envelope()
    invalid_headers = email_headers(raw, nonce="synthetic-email-invalid-0001", key="email-invalid")
    storage_headers = email_headers(raw, nonce="synthetic-email-storage-0001", key="email-storage")
    async with client_for(
        create_app(
            settings.model_copy(update={"document_max_bytes": len(EMAIL_PDF) - 1}),
            webhook_clock=lambda: NOW,
        )
    ) as client:
        invalid = await client.post(
            "/v1/invoices/email-webhook", headers=invalid_headers, content=raw
        )
    assert_problem(invalid, 413)
    assert nonce_count(migrated_database) == 0
    async with client_for(
        create_app(
            settings.model_copy(update={"raw_bucket": "missing-bucket"}), webhook_clock=lambda: NOW
        )
    ) as client:
        storage_failed = await client.post(
            "/v1/invoices/email-webhook", headers=storage_headers, content=raw
        )
    assert_problem(storage_failed, 503)
    assert nonce_count(migrated_database) == 0
    async with client_for(create_app(settings, webhook_clock=lambda: NOW)) as client:
        accepted = await client.post(
            "/v1/invoices/email-webhook", headers=invalid_headers, content=raw
        )
        duplicate = await client.post(
            "/v1/invoices/email-webhook", headers=storage_headers, content=raw
        )
    assert accepted.status_code == 201
    assert duplicate.status_code == 200
    assert nonce_count(migrated_database) == 2

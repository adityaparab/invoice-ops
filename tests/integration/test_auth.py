"""Role credentials, session lifecycle, and authorization against real PostgreSQL."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import psycopg
import pytest
from pydantic import SecretStr
from tests.integration.conftest import TEST_PASSWORD

from invoiceops_agent.api.app import create_app
from invoiceops_agent.api.auth_store import AuthStore, PostgresAuthStore
from invoiceops_agent.api.settings import ApiSettings
from invoiceops_agent.db.auth_seed import AuthSeedSettings, seed_auth_users

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
SESSION_SECRET = "synthetic-session-secret-0123456789abcdef"


def credentials(*, analyst_password: str = "synthetic-analyst-password") -> AuthSeedSettings:
    return AuthSeedSettings(
        analyst_email="analyst@example.test",
        analyst_password=SecretStr(analyst_password),
        manager_email="manager@example.test",
        manager_password=SecretStr("synthetic-manager-password"),
        auditor_email="auditor@example.test",
        auditor_password=SecretStr("synthetic-auditor-password"),
        platform_email="platform@example.test",
        platform_password=SecretStr("synthetic-platform-password"),
    )


@asynccontextmanager
async def client_for(
    settings: ApiSettings, auth_store: AuthStore | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings, auth_store=auth_store)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            yield client


def login_headers(key: str) -> dict[str, str]:
    return {"Idempotency-Key": key}


async def test_seed_is_idempotent_and_rotating_credentials_revokes_sessions(
    migrated_database: psycopg.Connection[tuple[object, ...]],
    migration_dsn: str,
    upload_settings: ApiSettings,
) -> None:
    first = credentials()
    seed_auth_users(migration_dsn, first)
    before = migrated_database.execute(
        "SELECT role, email, password_hash FROM auth_users ORDER BY role"
    ).fetchall()
    assert len(before) == 4
    seed_auth_users(migration_dsn, first)
    assert (
        migrated_database.execute(
            "SELECT role, email, password_hash FROM auth_users ORDER BY role"
        ).fetchall()
        == before
    )

    settings = upload_settings.model_copy(update={"auth_session_secret": SecretStr(SESSION_SECRET)})
    async with client_for(settings) as client:
        response = await client.post(
            "/v1/auth/login",
            json={"email": "analyst@example.test", "password": "synthetic-analyst-password"},
            headers=login_headers("auth-login-1"),
        )
        assert response.status_code == 200, response.text
        token = response.json()["token"]
        replay = await client.post(
            "/v1/auth/login",
            json={"email": "analyst@example.test", "password": "synthetic-analyst-password"},
            headers=login_headers("auth-login-1"),
        )
        assert replay.json()["token"] == token
        assert migrated_database.execute("SELECT count(*) FROM auth_sessions").fetchone() == (1,)
        assert (
            await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        ).json() == {"email": "analyst@example.test", "role": "ANALYST"}
        assert (
            await client.get("/v1/invoices", headers={"Authorization": f"Bearer {token}"})
        ).status_code == 200
        assert (
            await client.get("/v1/dashboard", headers={"Authorization": f"Bearer {token}"})
        ).status_code == 403

        rotated_secret = settings.model_copy(
            update={
                "auth_session_secret": SecretStr("different-synthetic-session-secret-0123456789")
            }
        )
        async with client_for(rotated_secret) as rotated_client:
            assert (
                await rotated_client.get(
                    "/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
                )
            ).status_code == 401

        seed_auth_users(migration_dsn, credentials(analyst_password="rotated-analyst-password"))
        assert (
            await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        ).status_code == 401
        assert migrated_database.execute("SELECT count(*) FROM auth_sessions").fetchone() == (0,)
        old = await client.post(
            "/v1/auth/login",
            json={"email": "analyst@example.test", "password": "synthetic-analyst-password"},
            headers=login_headers("auth-login-2"),
        )
        assert old.status_code == 401
        new = await client.post(
            "/v1/auth/login",
            json={"email": "analyst@example.test", "password": "rotated-analyst-password"},
            headers=login_headers("auth-login-3"),
        )
        assert new.status_code == 200
        assert new.json()["token"] != token


async def test_each_login_role_is_enforced_and_logout_revokes_only_that_session(
    migration_dsn: str,
    upload_settings: ApiSettings,
) -> None:
    seed_auth_users(migration_dsn, credentials())
    settings = upload_settings.model_copy(update={"auth_session_secret": SecretStr(SESSION_SECRET)})
    async with client_for(settings) as client:
        for role in ("ANALYST", "MANAGER", "AUDITOR", "PLATFORM"):
            email = f"{role.lower()}@example.test"
            response = await client.post(
                "/v1/auth/login",
                json={"email": email, "password": f"synthetic-{role.lower()}-password"},
                headers=login_headers(f"login-{role.lower()}"),
            )
            assert response.status_code == 200, response.text
            assert response.json()["role"] == role
            token = response.json()["token"]
            headers = {"Authorization": f"Bearer {token}"}
            queue = await client.get("/v1/invoices", headers=headers)
            assert queue.status_code == (403 if role in {"AUDITOR", "PLATFORM"} else 200)
            upload = await client.post(
                "/v1/invoices",
                headers={**headers, "Idempotency-Key": f"upload-{role}"},
                content=b"invalid-body",
            )
            assert upload.status_code == (415 if role == "ANALYST" else 403)
            assert (await client.get("/v1/evals/reports", headers=headers)).status_code == (
                403 if role in {"ANALYST", "MANAGER"} else 200
            )
            logout_headers = {**headers, "Idempotency-Key": f"logout-{role}"}
            assert (await client.post("/v1/auth/logout", headers=logout_headers)).status_code == 200
            assert (await client.post("/v1/auth/logout", headers=logout_headers)).status_code == 200
            assert (await client.get("/v1/auth/me", headers=headers)).status_code == 401

        bad = await client.post(
            "/v1/auth/login",
            json={"email": "missing@example.test", "password": TEST_PASSWORD},
            headers=login_headers("missing-user"),
        )
        assert bad.status_code == 401
        assert "missing@example.test" not in bad.text


async def test_lockout_and_session_expiry_use_injected_clock(
    migration_dsn: str,
    upload_settings: ApiSettings,
) -> None:
    seed_auth_users(migration_dsn, credentials())
    settings = upload_settings.model_copy(update={"auth_session_secret": SecretStr(SESSION_SECRET)})
    now = datetime.now(UTC)
    store = PostgresAuthStore(settings, clock=lambda: now)
    async with client_for(settings, store) as client:
        for attempt in range(5):
            denied = await client.post(
                "/v1/auth/login",
                json={"email": "manager@example.test", "password": "wrong-password"},
                headers=login_headers(f"wrong-{attempt}"),
            )
            assert denied.status_code == 401
        locked = await client.post(
            "/v1/auth/login",
            json={"email": "manager@example.test", "password": "synthetic-manager-password"},
            headers=login_headers("locked-manager"),
        )
        assert locked.status_code == 401
        now += timedelta(minutes=16)
        accepted = await client.post(
            "/v1/auth/login",
            json={"email": "manager@example.test", "password": "synthetic-manager-password"},
            headers=login_headers("unlocked-manager"),
        )
        assert accepted.status_code == 200
        token = accepted.json()["token"]
        now += timedelta(hours=8, seconds=1)
        assert (
            await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        ).status_code == 401

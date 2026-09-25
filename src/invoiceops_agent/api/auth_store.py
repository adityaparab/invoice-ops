"""Database-backed login sessions; only token digests are persisted."""

import hashlib
import hmac
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Protocol

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from invoiceops_agent.api.schemas.auth import LoginResponse, SessionUser
from invoiceops_agent.api.settings import ApiSettings
from invoiceops_agent.db.passwords import verify_password

logger = logging.getLogger(__name__)
_MAX_FAILURES = 5
_LOCK_MINUTES = 15


class AuthStore(Protocol):
    async def login(self, email: str, password: str, key: str) -> LoginResponse: ...
    async def resolve(self, token: str) -> SessionUser | None: ...
    async def logout(self, token: str) -> None: ...


def bearer_token(header_values: list[str]) -> str | None:
    if len(header_values) != 1:
        return None
    scheme, separator, token = header_values[0].partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token:
        return None
    return token


class PostgresAuthStore:
    def __init__(
        self, settings: ApiSettings, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._settings = settings
        self._clock = clock

    def _dsn(self) -> str:
        if self._settings.postgres_dsn is None or self._settings.auth_session_secret is None:
            raise HTTPException(503, "Login is not configured.")
        return self._settings.postgres_dsn.get_secret_value()

    async def login(self, email: str, password: str, key: str) -> LoginResponse:
        started = perf_counter()
        try:
            async with (
                await psycopg.AsyncConnection.connect(
                    self._dsn(),
                    connect_timeout=5,
                    row_factory=dict_row,
                    options="-c statement_timeout=10000 -c lock_timeout=10000",
                ) as connection,
                connection.transaction(),
            ):
                normalized = email.strip().lower()
                row = await (
                    await connection.execute(
                        "SELECT id, email, role, password_hash, failed_attempts, locked_until "
                        "FROM public.auth_users WHERE email = %s FOR UPDATE",
                        (normalized,),
                    )
                ).fetchone()
                valid = verify_password(password, row["password_hash"] if row else None)
                now = self._clock()
                if row is None or (row["locked_until"] is not None and row["locked_until"] > now):
                    raise HTTPException(401, "Invalid email or password.")
                if not valid:
                    failures = row["failed_attempts"] + 1
                    locked_until = (
                        now + timedelta(minutes=_LOCK_MINUTES)
                        if failures >= _MAX_FAILURES
                        else None
                    )
                    await connection.execute(
                        "UPDATE public.auth_users SET failed_attempts = %s, "
                        "locked_until = %s WHERE id = %s",
                        (failures, locked_until, row["id"]),
                    )
                    # Commit the failure counter before returning the generic error.
                    failure = True
                else:
                    failure = False
                    await connection.execute(
                        "UPDATE public.auth_users SET failed_attempts = 0, "
                        "locked_until = NULL WHERE id = %s",
                        (row["id"],),
                    )
                    secret = self._settings.auth_session_secret
                    assert secret is not None
                    token = (
                        "io_"
                        + hmac.new(
                            secret.get_secret_value().encode("utf-8"),
                            f"{row['id']}:{key}".encode(),
                            hashlib.sha256,
                        ).hexdigest()
                    )
                    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
                    fingerprint = hashlib.sha256(
                        secret.get_secret_value().encode("utf-8")
                    ).hexdigest()
                    expiry = now + timedelta(hours=self._settings.auth_session_hours)
                    await connection.execute(
                        "INSERT INTO public.auth_sessions "
                        "(token_hash, secret_fingerprint, user_id, expires_at) "
                        "VALUES (%s, %s, %s, %s) ON CONFLICT (token_hash) DO NOTHING",
                        (digest, fingerprint, row["id"], expiry),
                    )
                    session = await (
                        await connection.execute(
                            "SELECT expires_at FROM public.auth_sessions WHERE token_hash = %s",
                            (digest,),
                        )
                    ).fetchone()
                    assert session is not None
                    if session["expires_at"] <= now:
                        raise HTTPException(409, "Login attempt expired; retry with a new key.")
            if failure:
                raise HTTPException(401, "Invalid email or password.")
            assert row is not None
            assert session is not None
            logger.info(
                "event=auth.login role=%s duration_ms=%.1f",
                row["role"],
                (perf_counter() - started) * 1000,
            )
            return LoginResponse(
                email=row["email"], role=row["role"], token=token, expires_at=session["expires_at"]
            )
        except psycopg.Error as error:
            logger.error("event=auth.login.failed error_type=%s", type(error).__name__)
            raise HTTPException(503, "Login is temporarily unavailable.") from error

    async def resolve(self, token: str) -> SessionUser | None:
        if len(token) != 67 or not token.startswith("io_"):
            return None
        try:
            digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        except UnicodeEncodeError:
            return None
        try:
            secret = self._settings.auth_session_secret
            if secret is None:
                raise HTTPException(503, "Login is not configured.")
            async with await psycopg.AsyncConnection.connect(
                self._dsn(),
                connect_timeout=5,
                row_factory=dict_row,
                options="-c statement_timeout=5000",
            ) as connection:
                row = await (
                    await connection.execute(
                        "SELECT u.email, u.role FROM public.auth_sessions s "
                        "JOIN public.auth_users u ON u.id = s.user_id "
                        "WHERE s.token_hash = %s AND s.secret_fingerprint = %s "
                        "AND s.expires_at > %s",
                        (
                            digest,
                            hashlib.sha256(secret.get_secret_value().encode("utf-8")).hexdigest(),
                            self._clock(),
                        ),
                    )
                ).fetchone()
            return SessionUser.model_validate(row) if row is not None else None
        except psycopg.Error as error:
            logger.error("event=auth.resolve.failed error_type=%s", type(error).__name__)
            raise HTTPException(503, "Authentication is temporarily unavailable.") from error

    async def logout(self, token: str) -> None:
        if len(token) != 67 or not token.startswith("io_"):
            return
        try:
            digest = hashlib.sha256(token.encode("ascii")).hexdigest()
            async with await psycopg.AsyncConnection.connect(
                self._dsn(), connect_timeout=5
            ) as connection:
                await connection.execute(
                    "DELETE FROM public.auth_sessions WHERE token_hash = %s", (digest,)
                )
        except (psycopg.Error, UnicodeEncodeError) as error:
            logger.error("event=auth.logout.failed error_type=%s", type(error).__name__)
            raise HTTPException(503, "Logout is temporarily unavailable.") from error

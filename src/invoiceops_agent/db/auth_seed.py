"""Idempotently reconcile four environment-owned role accounts after migration."""

import logging
import re
from typing import Literal, Self

import psycopg
from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from invoiceops_agent.db.passwords import hash_password, verify_password

logger = logging.getLogger(__name__)
AuthRole = Literal["ANALYST", "MANAGER", "AUDITOR", "PLATFORM"]
ROLES: tuple[AuthRole, ...] = ("ANALYST", "MANAGER", "AUDITOR", "PLATFORM")


class AuthSeedSettings(BaseSettings):
    """All role credentials must be supplied; no fallback account is created."""

    model_config = SettingsConfigDict(
        env_prefix="INVOICEOPS_", extra="ignore", hide_input_in_errors=True
    )
    analyst_email: str
    analyst_password: SecretStr
    manager_email: str
    manager_password: SecretStr
    auditor_email: str
    auditor_password: SecretStr
    platform_email: str
    platform_password: SecretStr

    @field_validator("analyst_email", "manager_email", "auditor_email", "platform_email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        email = value.strip().lower()
        if len(email) > 254 or re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) is None:
            raise ValueError("Role email must be a valid email address")
        return email

    @field_validator(
        "analyst_password", "manager_password", "auditor_password", "platform_password"
    )
    @classmethod
    def validate_password(cls, value: SecretStr) -> SecretStr:
        password = value.get_secret_value()
        if len(password) < 12 or len(password) > 1024 or "\x00" in password:
            raise ValueError("Role password must contain 12 to 1024 characters and no NUL")
        return value

    @model_validator(mode="after")
    def distinct_emails(self) -> Self:
        if len(set(self.emails())) != len(ROLES):
            raise ValueError("Each role must have a different email address")
        return self

    def emails(self) -> tuple[str, ...]:
        return (self.analyst_email, self.manager_email, self.auditor_email, self.platform_email)

    def accounts(self) -> tuple[tuple[AuthRole, str, str], ...]:
        return tuple(
            (role, email, password.get_secret_value())
            for role, email, password in zip(
                ROLES,
                self.emails(),
                (
                    self.analyst_password,
                    self.manager_password,
                    self.auditor_password,
                    self.platform_password,
                ),
                strict=True,
            )
        )


def seed_auth_users(owner_dsn: str, settings: AuthSeedSettings) -> None:
    """Insert once; reruns preserve hashes/sessions until a credential actually changes."""
    url = make_url(owner_dsn).set(drivername="postgresql")
    with psycopg.connect(
        url.render_as_string(hide_password=False), connect_timeout=5
    ) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(761923, 8)")
        for role, email, password in settings.accounts():
            row = connection.execute(
                "SELECT id, email, password_hash FROM public.auth_users WHERE role = %s FOR UPDATE",
                (role,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO public.auth_users (role, email, password_hash) "
                    "VALUES (%s, %s, %s)",
                    (role, email, hash_password(password)),
                )
                logger.info("event=auth.seed.created role=%s", role)
            elif row[1] != email or not verify_password(password, row[2]):
                connection.execute(
                    "UPDATE public.auth_users SET email = %s, password_hash = %s, "
                    "failed_attempts = 0, locked_until = NULL WHERE id = %s",
                    (email, hash_password(password), row[0]),
                )
                connection.execute("DELETE FROM public.auth_sessions WHERE user_id = %s", (row[0],))
                logger.info("event=auth.seed.rotated role=%s", role)

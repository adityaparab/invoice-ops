"""Role-bound read tokens for the synthetic AP, manager, and audit personas."""

import hmac
from typing import Literal

from fastapi import HTTPException, Request
from pydantic import SecretStr

from invoiceops_agent.api.auth_store import bearer_token
from invoiceops_agent.api.schemas.auth import SessionUser
from invoiceops_agent.api.settings import ApiSettings
from invoiceops_agent.api.uploads import authenticate_upload

ReadRole = Literal["ANALYST", "MANAGER", "AUDITOR"]


async def session_user(request: Request) -> SessionUser | None:
    token = bearer_token(request.headers.getlist("Authorization"))
    if token is None or not token.startswith("io_"):
        return None
    user: SessionUser | None = await request.app.state.auth_store.resolve(token)
    if user is None:
        raise HTTPException(401, "Login session is invalid or expired.")
    return user


async def authenticate_read(request: Request, settings: ApiSettings) -> ReadRole:
    user = await session_user(request)
    if user is not None:
        if user.role == "PLATFORM":
            raise HTTPException(403, "This role cannot read operational invoices.")
        return user.role
    credentials: tuple[tuple[ReadRole, SecretStr | None], ...] = (
        ("ANALYST", settings.analyst_token),
        ("MANAGER", settings.manager_token),
        ("AUDITOR", settings.auditor_token),
    )
    if not any(token is not None for _, token in credentials):
        raise HTTPException(503, "Invoice read access is not configured.")
    supplied = request.headers.getlist("Authorization")
    if len(supplied) != 1:
        raise HTTPException(
            401, "A valid role token is required.", headers={"WWW-Authenticate": "Bearer"}
        )
    candidate = supplied[0].encode("utf-8")
    for role, token in credentials:
        if token is not None and hmac.compare_digest(
            candidate, f"Bearer {token.get_secret_value()}".encode("ascii")
        ):
            return role
    raise HTTPException(
        401, "A valid role token is required.", headers={"WWW-Authenticate": "Bearer"}
    )


def authorize_queue(role: ReadRole) -> None:
    if role == "AUDITOR":
        raise HTTPException(403, "This role cannot list the operational invoice queue.")


def authorize_auditor(role: ReadRole) -> None:
    if role != "AUDITOR":
        raise HTTPException(403, "Only the auditor can read full provenance.")


async def authenticate_run(request: Request, settings: ApiSettings) -> None:
    """Operational personas and the upload service may inspect bounded run progress."""
    if await session_user(request) is not None:
        return
    try:
        await authenticate_read(request, settings)
    except HTTPException as error:
        if error.status_code not in {401, 503}:
            raise
        if settings.service_token is None and error.status_code == 401:
            raise
        await authenticate_upload(request, settings)


async def authenticate_evals(request: Request, settings: ApiSettings) -> None:
    """Evaluation reports are visible to the auditor and platform service only."""
    user = await session_user(request)
    if user is not None:
        if user.role not in {"AUDITOR", "PLATFORM"}:
            raise HTTPException(403, "Only the auditor or platform service can read evaluations.")
        return
    try:
        role = await authenticate_read(request, settings)
    except HTTPException as error:
        if error.status_code not in {401, 503}:
            raise
        if settings.service_token is None and error.status_code == 401:
            raise
        await authenticate_upload(request, settings)
    else:
        if role != "AUDITOR":
            raise HTTPException(403, "Only the auditor or platform service can read evaluations.")

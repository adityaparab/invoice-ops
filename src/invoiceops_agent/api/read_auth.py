"""Role-bound read tokens for the synthetic AP, manager, and audit personas."""

import hmac
from typing import Literal

from fastapi import HTTPException, Request
from pydantic import SecretStr

from invoiceops_agent.api.settings import ApiSettings
from invoiceops_agent.api.uploads import authenticate_upload

ReadRole = Literal["ANALYST", "MANAGER", "AUDITOR"]


def authenticate_read(request: Request, settings: ApiSettings) -> ReadRole:
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


def authenticate_run(request: Request, settings: ApiSettings) -> None:
    """Operational personas and the upload service may inspect bounded run progress."""
    try:
        authenticate_read(request, settings)
    except HTTPException as error:
        if error.status_code not in {401, 503}:
            raise
        if settings.service_token is None and error.status_code == 401:
            raise
        authenticate_upload(request, settings)

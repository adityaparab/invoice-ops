"""Login session wire contracts."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field

AuthRole = Literal["ANALYST", "MANAGER", "AUDITOR", "PLATFORM"]


class LoginRequest(BaseModel):
    email: str = Field(min_length=4, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


class SessionUser(BaseModel):
    email: str
    role: AuthRole


class LoginResponse(SessionUser):
    token: str
    expires_at: AwareDatetime


class LogoutResponse(BaseModel):
    status: Literal["signed_out"] = "signed_out"

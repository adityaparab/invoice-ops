"""Versioned password hashing for locally provisioned role accounts."""

import hashlib
import hmac
import secrets

_N = 1 << 14
_R = 8
_P = 1
_DUMMY = f"scrypt${_N}${_R}${_P}$" + "00" * 16 + "$" + "00" * 32


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str | None) -> bool:
    """Do comparable work for an unknown account and reject unknown hash formats."""
    candidate = encoded if encoded is not None else _DUMMY
    try:
        algorithm, n, r, p, salt, expected = candidate.split("$")
        if algorithm != "scrypt" or (int(n), int(r), int(p)) != (_N, _R, _P):
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt), n=_N, r=_R, p=_P, dklen=32
        )
        matched = hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False
    return matched and encoded is not None

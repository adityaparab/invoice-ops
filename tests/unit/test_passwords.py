"""Password hash checks require no network or database."""

import pytest

from invoiceops_agent.db.passwords import hash_password, verify_password

pytestmark = pytest.mark.unit


def test_hash_uses_unique_salts_and_rejects_unknown_credentials() -> None:
    first = hash_password("synthetic-password-2026")
    second = hash_password("synthetic-password-2026")
    assert first != second
    assert "synthetic-password" not in first
    assert verify_password("synthetic-password-2026", first)
    assert not verify_password("wrong-password", first)
    assert not verify_password("synthetic-password-2026", None)
    assert not verify_password("synthetic-password-2026", "invalid")

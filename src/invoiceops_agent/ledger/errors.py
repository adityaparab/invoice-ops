"""Sanitized ledger failures with explicit request correlation."""

from uuid import UUID


class LedgerError(Exception):
    def __init__(self, detail: str, *, run_id: UUID | None, trace_id: str) -> None:
        self.run_id = run_id
        self.trace_id = trace_id
        super().__init__(f"{detail} run_id={run_id} trace_id={trace_id}")


class LedgerTransactionRequired(LedgerError):
    """The append caller has not opened a usable transaction."""


class LedgerIdentityMismatch(LedgerError):
    """Run, invoice, graph version, or superseded event identities do not agree."""


class LedgerRunNotFound(LedgerError):
    """The referenced run does not exist in the caller's transaction."""


class LedgerConflict(LedgerError):
    """The append would violate a persisted identity or uniqueness constraint."""


class LedgerStorageError(LedgerError):
    """Storage failed; messages omit connection and payload details."""


class LedgerCursorMismatch(LedgerError):
    """A cursor belongs to a different history scope."""

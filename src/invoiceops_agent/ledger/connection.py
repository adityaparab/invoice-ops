"""Minimal async psycopg-compatible seam for deterministic ledger tests."""

from collections.abc import Mapping, Sequence
from typing import LiteralString, Protocol

from psycopg.pq import TransactionStatus


class LedgerCursor(Protocol):
    async def fetchone(self) -> Mapping[str, object] | None: ...

    async def fetchall(self) -> Sequence[Mapping[str, object]]: ...


class ConnectionInfo(Protocol):
    @property
    def transaction_status(self) -> TransactionStatus: ...


class LedgerConnection(Protocol):
    @property
    def info(self) -> ConnectionInfo: ...

    async def execute(
        self, query: LiteralString, params: Sequence[object] | None = None
    ) -> LedgerCursor: ...

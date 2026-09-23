"""Offline ledger behavior with an explicit transaction and canned async query results."""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import LiteralString
from uuid import UUID

import psycopg
import pytest
from psycopg.pq import TransactionStatus
from pydantic import ValidationError

from invoiceops_agent.ledger.errors import (
    LedgerConflict,
    LedgerCursorMismatch,
    LedgerIdentityMismatch,
    LedgerRunNotFound,
    LedgerStorageError,
    LedgerTransactionRequired,
)
from invoiceops_agent.ledger.reader import LedgerReader
from invoiceops_agent.ledger.schemas import AppendEvent, InvoiceCursor, RunCursor, VersionOverrides
from invoiceops_agent.ledger.settings import LedgerSettings
from invoiceops_agent.ledger.writer import LedgerWriter

pytestmark = pytest.mark.unit
RUN_ID = UUID(int=1)
INVOICE_ID = UUID(int=2)
EVENT_ID = UUID(int=3)
TRACE_ID = "a" * 32
CREATED_AT = datetime(2026, 9, 23, 12, tzinfo=UTC)


@dataclass
class FakeInfo:
    transaction_status: TransactionStatus = TransactionStatus.INTRANS


@dataclass
class FakeCursor:
    rows: Sequence[Mapping[str, object]]

    async def fetchone(self) -> Mapping[str, object] | None:
        return self.rows[0] if self.rows else None

    async def fetchall(self) -> Sequence[Mapping[str, object]]:
        return self.rows


@dataclass
class FakeConnection:
    results: list[Sequence[Mapping[str, object]] | Exception]
    info: FakeInfo = field(default_factory=FakeInfo)
    calls: list[tuple[str, Sequence[object] | None]] = field(default_factory=list)

    async def execute(
        self, query: LiteralString, params: Sequence[object] | None = None
    ) -> FakeCursor:
        self.calls.append((query, params))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return FakeCursor(result)


def settings() -> LedgerSettings:
    return LedgerSettings(
        graph_version="graph@v1",
        model_version="not-applicable@v1",
        prompt_version="not-applicable@v1",
        policy_version="not-applicable@v1",
    )


def command(**changes: object) -> AppendEvent:
    return AppendEvent.model_validate(
        {
            "run_id": RUN_ID,
            "invoice_id": INVOICE_ID,
            "event_type": "ingest.accepted",
            "actor_type": "SYSTEM",
            "actor_id": "synthetic-service",
            "payload": {"amount": "12.34"},
            **changes,
        }
    )


def identity(**changes: object) -> dict[str, object]:
    return {
        "invoice_id": INVOICE_ID,
        "graph_version": "graph@v1",
        "trace_id": TRACE_ID,
        **changes,
    }


def event_row(sequence: int = 1) -> dict[str, object]:
    return {
        **command().model_dump(exclude={"versions"}),
        **settings().model_dump(),
        "id": UUID(int=sequence + 2),
        "sequence": sequence,
        "created_at": CREATED_AT,
    }


@pytest.mark.asyncio
async def test_append_stages_pinned_event_and_preserves_caller_transaction(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    connection = FakeConnection([[identity()], [{"sequence": 1}], []])
    writer = LedgerWriter(settings(), clock=lambda: CREATED_AT, new_id=lambda: EVENT_ID)
    event = await writer.append(connection, command(), trace_id=TRACE_ID)
    assert event.id == EVENT_ID
    assert event.sequence == 1
    assert event.created_at == CREATED_AT
    assert event.versions.model_version == "not-applicable@v1"
    assert connection.calls[0][0].endswith("FOR UPDATE")
    assert "public.runs" in connection.calls[0][0]
    assert connection.calls[2][0].startswith("INSERT INTO public.ledger")
    assert all("UPDATE public.ledger" not in sql for sql, _ in connection.calls)
    assert connection.info.transaction_status is TransactionStatus.INTRANS
    assert "ledger_append_staged" in caplog.text
    assert f"run_id={RUN_ID}" in caplog.text
    assert f"trace_id={TRACE_ID}" in caplog.text
    assert "duration_ms=" in caplog.text


@pytest.mark.asyncio
async def test_event_can_override_specific_version_pins_and_normalizes_clock() -> None:
    connection = FakeConnection([[identity()], [{"sequence": 2}], []])
    now = datetime(2026, 9, 23, 14, tzinfo=timezone(timedelta(hours=2)))
    writer = LedgerWriter(settings(), clock=lambda: now, new_id=lambda: EVENT_ID)
    event = await writer.append(
        connection,
        command(
            versions=VersionOverrides(model_version="extract-model@v2", prompt_version="extract@v2")
        ),
        trace_id=TRACE_ID,
    )
    assert event.versions.graph_version == "graph@v1"
    assert event.versions.model_version == "extract-model@v2"
    assert event.versions.prompt_version == "extract@v2"
    assert event.versions.policy_version == "not-applicable@v1"
    assert event.created_at == CREATED_AT
    assert event.created_at.tzinfo is UTC


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [TransactionStatus.IDLE, TransactionStatus.INERROR, TransactionStatus.ACTIVE]
)
async def test_append_requires_a_usable_caller_transaction(status: TransactionStatus) -> None:
    connection = FakeConnection([], info=FakeInfo(status))
    with pytest.raises(LedgerTransactionRequired):
        await LedgerWriter(settings()).append(connection, command(), trace_id=TRACE_ID)
    assert connection.calls == []


@pytest.mark.asyncio
async def test_unknown_run_has_a_typed_error() -> None:
    connection = FakeConnection([[]])
    with pytest.raises(LedgerRunNotFound):
        await LedgerWriter(settings()).append(connection, command(), trace_id=TRACE_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "run_identity", [identity(invoice_id=UUID(int=99)), identity(graph_version="other@v2")]
)
async def test_invoice_and_graph_version_must_match_run(run_identity: dict[str, object]) -> None:
    connection = FakeConnection([[run_identity]])
    with pytest.raises(LedgerIdentityMismatch):
        await LedgerWriter(settings()).append(connection, command(), trace_id=TRACE_ID)
    assert len(connection.calls) == 1


@pytest.mark.asyncio
async def test_supersession_must_reference_the_same_run_and_invoice() -> None:
    connection = FakeConnection(
        [[identity()], [{"run_id": UUID(int=99), "invoice_id": INVOICE_ID}]]
    )
    with pytest.raises(LedgerIdentityMismatch):
        await LedgerWriter(settings()).append(
            connection, command(supersedes_id=UUID(int=8)), trace_id=TRACE_ID
        )
    assert len(connection.calls) == 2


@pytest.mark.asyncio
async def test_valid_supersession_is_an_append_with_a_new_sequence() -> None:
    connection = FakeConnection(
        [[identity()], [{"run_id": RUN_ID, "invoice_id": INVOICE_ID}], [{"sequence": 2}], []]
    )
    event = await LedgerWriter(settings()).append(
        connection, command(supersedes_id=UUID(int=8)), trace_id=TRACE_ID
    )
    assert event.sequence == 2
    assert event.supersedes_id == UUID(int=8)
    assert connection.calls[-1][0].startswith("INSERT INTO")


@pytest.mark.asyncio
async def test_constraint_conflicts_are_not_retried() -> None:
    connection = FakeConnection(
        [[identity()], [{"sequence": 1}], psycopg.IntegrityError("private")]
    )
    with pytest.raises(LedgerConflict):
        await LedgerWriter(settings()).append(connection, command(), trace_id=TRACE_ID)
    assert len(connection.calls) == 3


@pytest.mark.asyncio
async def test_storage_failure_omits_sensitive_error_messages(
    caplog: pytest.LogCaptureFixture,
) -> None:
    connection = FakeConnection([psycopg.OperationalError("private-secret")])
    with pytest.raises(LedgerStorageError) as error:
        await LedgerWriter(settings()).append(connection, command(), trace_id=TRACE_ID)
    assert "private-secret" not in str(error.value) + caplog.text
    assert f"run_id={RUN_ID}" in caplog.text


@pytest.mark.asyncio
async def test_naive_clock_cannot_enter_the_audit_trail() -> None:
    connection = FakeConnection([[identity()], [{"sequence": 1}]])
    writer = LedgerWriter(settings(), clock=lambda: datetime(2026, 9, 23))
    with pytest.raises(LedgerStorageError):
        await writer.append(connection, command(), trace_id=TRACE_ID)
    assert len(connection.calls) == 2


@pytest.mark.asyncio
async def test_run_reader_uses_one_bounded_query_and_returns_continuation() -> None:
    connection = FakeConnection([[event_row(1), event_row(2), event_row(3)]])
    page = await LedgerReader().for_run(connection, RUN_ID, trace_id=TRACE_ID, limit=2)
    assert [event.sequence for event in page.events] == [1, 2]
    assert page.next_cursor == RunCursor(run_id=RUN_ID, sequence=2)
    assert len(connection.calls) == 1
    assert connection.calls[0][1] == (RUN_ID, 0, 3)
    assert "ORDER BY sequence ASC LIMIT" in connection.calls[0][0]


@pytest.mark.asyncio
async def test_run_reader_continues_after_cursor_without_offset() -> None:
    connection = FakeConnection([[event_row(3)]])
    page = await LedgerReader().for_run(
        connection, RUN_ID, trace_id=TRACE_ID, limit=2, after=RunCursor(run_id=RUN_ID, sequence=2)
    )
    assert [event.sequence for event in page.events] == [3]
    assert page.next_cursor is None
    assert connection.calls[0][1] == (RUN_ID, 2, 3)


@pytest.mark.asyncio
async def test_invoice_reader_uses_timestamp_and_id_tiebreaker() -> None:
    connection = FakeConnection([[event_row(1), event_row(2)]])
    reader = LedgerReader()
    page = await reader.for_invoice(connection, INVOICE_ID, trace_id=TRACE_ID, limit=1)
    assert page.next_cursor == InvoiceCursor(
        invoice_id=INVOICE_ID, created_at=CREATED_AT, id=EVENT_ID
    )
    next_connection = FakeConnection([[event_row(2)]])
    next_page = await reader.for_invoice(
        next_connection, INVOICE_ID, trace_id=TRACE_ID, limit=1, after=page.next_cursor
    )
    assert next_page.next_cursor is None
    assert next_connection.calls[0][1] == (INVOICE_ID, CREATED_AT, EVENT_ID, 2)
    assert "(created_at, id) > (%s, %s)" in next_connection.calls[0][0]


@pytest.mark.asyncio
async def test_readers_return_empty_pages_for_unknown_scopes() -> None:
    reader = LedgerReader()
    assert (await reader.for_run(FakeConnection([[]]), RUN_ID, trace_id=TRACE_ID)).events == []
    assert (
        await reader.for_invoice(FakeConnection([[]]), INVOICE_ID, trace_id=TRACE_ID)
    ).events == []


@pytest.mark.asyncio
async def test_cursors_cannot_cross_history_scopes() -> None:
    connection = FakeConnection([])
    reader = LedgerReader()
    with pytest.raises(LedgerCursorMismatch):
        await reader.for_run(
            connection, RUN_ID, trace_id=TRACE_ID, after=RunCursor(run_id=UUID(int=99), sequence=1)
        )
    with pytest.raises(LedgerCursorMismatch):
        await reader.for_invoice(
            connection,
            INVOICE_ID,
            trace_id=TRACE_ID,
            after=InvoiceCursor(invoice_id=UUID(int=99), created_at=CREATED_AT, id=EVENT_ID),
        )
    assert connection.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, 201])
async def test_page_limits_are_bounded_before_querying(limit: int) -> None:
    connection = FakeConnection([])
    with pytest.raises(ValidationError):
        await LedgerReader().for_run(connection, RUN_ID, trace_id=TRACE_ID, limit=limit)
    assert connection.calls == []


@pytest.mark.asyncio
async def test_invalid_persisted_rows_fail_with_sanitized_storage_error() -> None:
    connection = FakeConnection([[{"payload": "private-secret"}]])
    with pytest.raises(LedgerStorageError) as error:
        await LedgerReader().for_run(connection, RUN_ID, trace_id=TRACE_ID)
    assert "private-secret" not in str(error.value)


def test_all_version_pins_are_required_and_environment_backed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = ("GRAPH", "MODEL", "PROMPT", "POLICY")
    for name in names:
        monkeypatch.delenv(f"INVOICEOPS_LEDGER_{name}_VERSION", raising=False)
    with pytest.raises(ValidationError):
        LedgerSettings()
    for name in names:
        monkeypatch.setenv(f"INVOICEOPS_LEDGER_{name}_VERSION", f"{name.lower()}@v2")
    resolved = LedgerSettings().resolve()
    assert resolved.graph_version == "graph@v2"
    assert resolved.policy_version == "policy@v2"
    with pytest.raises(ValidationError):
        VersionOverrides(model_version="   ")


def test_payload_requires_json_and_preserves_decimal_as_strings() -> None:
    assert command().payload["amount"] == "12.34"
    with pytest.raises(ValidationError):
        command(payload={"amount": Decimal("12.34")})
    with pytest.raises(ValidationError):
        command(payload={"amount": float("nan")})

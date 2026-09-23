"""Validation outcomes are returned only after one successful audit attempt."""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

import pytest
from tests.unit.test_validation import invoice, strict_usd

from invoiceops_agent.graph.nodes.validate import ValidateNode
from invoiceops_agent.ledger.errors import LedgerStorageError
from invoiceops_agent.ledger.schemas import AppendEvent, LedgerEvent, VersionPins
from invoiceops_agent.schemas.validation import ValidationRequest

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
TRACE_ID = "b" * 32


@dataclass
class CaptureAudit:
    commands: list[AppendEvent] = field(default_factory=list)
    failure: Exception | None = None
    blocked: asyncio.Event | None = None

    async def append(self, command: AppendEvent, *, trace_id: str) -> LedgerEvent:
        assert trace_id == TRACE_ID
        self.commands.append(command)
        if self.blocked is not None:
            await self.blocked.wait()
        if self.failure is not None:
            raise self.failure
        assert command.versions is not None
        return LedgerEvent(
            **command.model_dump(exclude={"versions"}),
            id=UUID(int=3),
            sequence=1,
            versions=VersionPins.model_validate(
                {
                    **command.versions.model_dump(),
                    "graph_version": "graph@v1",
                }
            ),
            created_at=datetime(2026, 9, 23, tzinfo=UTC),
        )


def request(*, valid: bool = True) -> ValidationRequest:
    return ValidationRequest(
        run_id=UUID(int=1),
        invoice_id=UUID(int=2),
        trace_id=TRACE_ID,
        extraction=invoice(total_amount="24" if valid else "99"),
    )


@pytest.mark.parametrize("valid", [True, False])
async def test_success_and_business_failure_each_get_one_policy_event(valid: bool) -> None:
    audit = CaptureAudit()
    result = await ValidateNode(audit, strict_usd()).run(request(valid=valid))
    assert result.status == ("PASS" if valid else "FAIL")
    assert len(audit.commands) == 1
    event = audit.commands[0]
    assert (event.actor_type, event.event_type, event.node) == (
        "POLICY",
        "validation.completed",
        "Validate",
    )
    assert event.payload == result.model_dump(mode="json")
    assert event.versions is not None and event.versions.policy_version == "synthetic-strict@v1"
    assert event.versions.model_version == event.versions.prompt_version == "not-applicable@v1"
    assert "Synthetic Supplier" not in event.model_dump_json()


async def test_failed_audit_propagates_without_retry_or_returned_decision(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failure = LedgerStorageError("synthetic-private-cause", run_id=UUID(int=1), trace_id=TRACE_ID)
    audit = CaptureAudit(failure=failure)
    with pytest.raises(LedgerStorageError) as captured:
        await ValidateNode(audit).run(request())
    assert captured.value is failure
    assert len(audit.commands) == 1
    assert "synthetic-private-cause" not in caplog.text
    assert "validation_completed" not in caplog.text


async def test_audit_completion_is_required_and_cancellation_propagates() -> None:
    audit = CaptureAudit(blocked=asyncio.Event())
    task = asyncio.create_task(ValidateNode(audit).run(request()))
    await asyncio.sleep(0)
    assert len(audit.commands) == 1 and not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(audit.commands) == 1

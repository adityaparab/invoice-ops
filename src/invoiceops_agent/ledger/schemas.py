"""Typed append commands, immutable event views, and bounded pagination contracts."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

Version = Annotated[str, Field(min_length=1, max_length=128, pattern=r"\S")]
TraceId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
ActorType = Literal["SYSTEM", "AGENT", "HUMAN", "POLICY"]


def normalize_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


UtcDatetime = Annotated[AwareDatetime, AfterValidator(normalize_utc)]


class LedgerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class VersionPins(LedgerModel):
    graph_version: Version
    model_version: Version
    prompt_version: Version
    policy_version: Version


class VersionOverrides(LedgerModel):
    graph_version: Version | None = None
    model_version: Version | None = None
    prompt_version: Version | None = None
    policy_version: Version | None = None


class AppendEvent(LedgerModel):
    run_id: UUID
    invoice_id: UUID
    event_type: Version
    actor_type: ActorType
    actor_id: Version
    payload: dict[str, JsonValue]
    node: Version | None = None
    supersedes_id: UUID | None = None
    versions: VersionOverrides | None = None


class LedgerEvent(LedgerModel):
    id: UUID
    run_id: UUID
    invoice_id: UUID
    sequence: int = Field(ge=1)
    event_type: Version
    actor_type: ActorType
    actor_id: Version
    payload: dict[str, JsonValue]
    node: Version | None
    supersedes_id: UUID | None
    versions: VersionPins
    created_at: UtcDatetime


class RunCursor(LedgerModel):
    run_id: UUID
    sequence: int = Field(ge=1)


class InvoiceCursor(LedgerModel):
    invoice_id: UUID
    created_at: UtcDatetime
    id: UUID


class RunPage(LedgerModel):
    events: list[LedgerEvent] = Field(max_length=200)
    next_cursor: RunCursor | None


class InvoicePage(LedgerModel):
    events: list[LedgerEvent] = Field(max_length=200)
    next_cursor: InvoiceCursor | None


class RunIdentity(LedgerModel):
    invoice_id: UUID
    graph_version: Version
    trace_id: TraceId


class NextSequence(LedgerModel):
    sequence: int = Field(ge=1)


class EventIdentity(LedgerModel):
    invoice_id: UUID
    run_id: UUID


class LedgerContext(LedgerModel):
    trace_id: TraceId


class ReadQuery(LedgerContext):
    limit: int = Field(default=50, ge=1, le=200)

"""Manager dashboard totals with explicit observation coverage."""

from datetime import date
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from invoiceops_agent.schemas.common import ExactDecimal


class DashboardModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class DailyVolume(DashboardModel):
    day: date
    invoices: int = Field(ge=0)


class ExceptionCount(DashboardModel):
    code: str = Field(min_length=1, max_length=128)
    count: int = Field(ge=0)


class AgingCounts(DashboardModel):
    under_24_hours: int = Field(ge=0)
    one_to_three_days: int = Field(ge=0)
    over_three_days: int = Field(ge=0)
    sla_overdue: int = Field(ge=0)


class DashboardSummary(DashboardModel):
    as_of: AwareDatetime
    period_days: int = Field(ge=1, le=90)
    invoice_count: int = Field(ge=0)
    resolved_count: int = Field(ge=0)
    auto_approved_count: int = Field(ge=0)
    stp_rate: ExactDecimal | None
    open_exception_count: int = Field(ge=0)
    aging: AgingCounts
    cost_observed_invoices: int = Field(ge=0)
    total_observed_cost_usd: ExactDecimal | None
    cost_per_observed_invoice_usd: ExactDecimal | None
    volume_by_day: tuple[DailyVolume, ...]
    exception_types: tuple[ExceptionCount, ...]
    cost_coverage: Literal["COMPLETE", "PARTIAL", "UNAVAILABLE"]

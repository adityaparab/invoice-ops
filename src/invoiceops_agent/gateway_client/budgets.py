"""Advisory per-run cost alerts from LiteLLM response-cost metadata."""

from collections import OrderedDict
from decimal import Decimal
from uuid import UUID


class BudgetAlertTracker:
    def __init__(self, threshold_usd: Decimal, *, max_runs: int = 10_000) -> None:
        self._threshold = threshold_usd
        self._max_runs = max_runs
        self._totals: OrderedDict[UUID, Decimal] = OrderedDict()
        self._alerted: set[UUID] = set()

    def observe(self, run_id: UUID, cost_usd: Decimal | None) -> tuple[Decimal | None, bool]:
        if cost_usd is None:
            return self._totals.get(run_id), False
        total = self._totals.get(run_id, Decimal(0)) + cost_usd
        self._totals[run_id] = total
        self._totals.move_to_end(run_id)
        if len(self._totals) > self._max_runs:
            expired, _ = self._totals.popitem(last=False)
            self._alerted.discard(expired)
        alert = total >= self._threshold and run_id not in self._alerted
        if alert:
            self._alerted.add(run_id)
        return total, alert

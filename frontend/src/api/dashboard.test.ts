import { afterEach, expect, test, vi } from "vitest";
import { fetchDashboard } from "./dashboard";

afterEach(() => vi.unstubAllGlobals());

const summary = {
  as_of: "2030-01-15T12:00:00Z", period_days: 1,
  invoice_count: 2, resolved_count: 1, auto_approved_count: 1, stp_rate: "1",
  open_exception_count: 1,
  aging: { under_24_hours: 0, one_to_three_days: 1, over_three_days: 0, sla_overdue: 1 },
  cost_observed_invoices: 2, total_observed_cost_usd: "0.06",
  cost_per_observed_invoice_usd: "0.03", cost_coverage: "COMPLETE",
  volume_by_day: [{ day: "2030-01-15", invoices: 2 }],
  exception_types: [{ code: "PRICE_VARIANCE", count: 1 }],
};

test("manager dashboard parses exact decimal strings and sends the role token", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(summary), {
    status: 200, headers: { "content-type": "application/json" },
  }));
  vi.stubGlobal("fetch", fetchMock);
  await expect(fetchDashboard("synthetic-manager-token", 1)).resolves.toEqual(summary);
  const request = fetchMock.mock.calls[0]?.[0] as Request;
  expect(request.headers.get("Authorization")).toBe("Bearer synthetic-manager-token");
  expect(request.url).toContain("period_days=1");
});

test("dashboard rejects a numeric money value at the wire boundary", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
    ...summary, cost_per_observed_invoice_usd: 0.03,
  }), { status: 200, headers: { "content-type": "application/json" } })));
  await expect(fetchDashboard("synthetic-manager-token", 1)).rejects.toThrow();
});

import { afterEach, expect, test, vi } from "vitest";
import { fetchRunProgress } from "./runs";

afterEach(() => vi.unstubAllGlobals());

const runId = "00000000-0000-4000-8000-000000000051";
const invoiceId = "00000000-0000-4000-8000-000000000052";
const names = [
  "Ingest", "Extract", "Validate", "Match3Way", "Policy", "Gate",
  "AutoApprove", "ExceptionTriage", "HumanReview", "Archive", "Reject",
];
const progress = {
  run_id: runId,
  invoice_id: invoiceId,
  status: "RUNNING",
  graph_version: "invoice-v1",
  active_node: "Extract",
  progress_source: "audit-ledger",
  nodes: names.map((name) => ({
    name,
    observed_at: name === "Ingest" ? "2026-09-24T12:00:00Z" : null,
    event_type: name === "Ingest" ? "ingest.accepted" : null,
    state: name === "Ingest" ? { source: "UPLOAD" } : {},
  })),
  started_at: "2026-09-24T12:00:00Z",
  completed_at: null,
  read_at: "2026-09-24T12:00:01Z",
};

test("run progress polls a typed role-protected response", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(progress), {
    status: 200, headers: { "content-type": "application/json" },
  }));
  vi.stubGlobal("fetch", fetchMock);
  await expect(fetchRunProgress("synthetic-analyst-token", runId)).resolves.toEqual(progress);
  const request = fetchMock.mock.calls[0]?.[0] as Request;
  expect(request.headers.get("Authorization")).toBe("Bearer synthetic-analyst-token");
  expect(request.url).toContain(`/v1/runs/${runId}/progress`);
});

test("run progress refuses an unexpected state field", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
    ...progress, raw_ref: "secret-storage-key",
  }), { status: 200, headers: { "content-type": "application/json" } })));
  await expect(fetchRunProgress("synthetic-analyst-token", runId)).rejects.toThrow();
});

import { afterEach, expect, test, vi } from "vitest";
import { fetchInvoiceProvenancePage, fetchRunTracePage } from "./provenance";

afterEach(() => vi.unstubAllGlobals());

const runId = "00000000-0000-4000-8000-000000000071";
const invoiceId = "00000000-0000-4000-8000-000000000072";
const eventId = "00000000-0000-4000-8000-000000000073";
const createdAt = "2026-09-24T12:00:00Z";
const event = {
  id: eventId, run_id: runId, invoice_id: invoiceId, sequence: 1,
  event_type: "synthetic.audit", node: "Validate", actor_type: "POLICY",
  actor_id: "synthetic-policy", payload: { status: "PASS" }, supersedes_id: null,
  versions: {
    graph_version: "graph@v1", model_version: "not-applicable@v1",
    prompt_version: "not-applicable@v1", policy_version: "policy@v1",
  },
  created_at: createdAt,
} as const;

test("run trace parses ordered metadata without a ledger payload", async () => {
  const { payload: _payload, run_id: _runId, invoice_id: _invoiceId, ...traceEvent } = event;
  const page = {
    run_id: runId, invoice_id: invoiceId, trace_id: "a".repeat(32),
    status: "PAUSED", graph_version: "graph@v1", started_at: null,
    completed_at: null, events: [traceEvent], next_cursor: null,
  };
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(page), {
    status: 200, headers: { "content-type": "application/json" },
  }));
  vi.stubGlobal("fetch", fetchMock);
  await expect(fetchRunTracePage("synthetic-auditor-token", runId, 3)).resolves.toEqual(page);
  const request = fetchMock.mock.calls[0]?.[0] as Request;
  expect(request.headers.get("Authorization")).toBe("Bearer synthetic-auditor-token");
  expect(request.url).toContain("after_sequence=3");
  expect(request.url).toContain("limit=100");
});

test("invoice provenance parses cross-run ledger and sends both cursor fields", async () => {
  const page = {
    invoice_id: invoiceId, status: "NEEDS_REVIEW", source: "UPLOAD",
    created_at: createdAt, events: [event], next_cursor: null,
  };
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(page), {
    status: 200, headers: { "content-type": "application/json" },
  }));
  vi.stubGlobal("fetch", fetchMock);
  await expect(fetchInvoiceProvenancePage(
    "synthetic-auditor-token", invoiceId, { created_at: createdAt, id: eventId },
  )).resolves.toEqual(page);
  const request = fetchMock.mock.calls[0]?.[0] as Request;
  expect(request.headers.get("Authorization")).toBe("Bearer synthetic-auditor-token");
  expect(request.url).toContain("after_created_at=");
  expect(request.url).toContain(`after_event_id=${eventId}`);
});

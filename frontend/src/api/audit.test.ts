import { afterEach, expect, test, vi } from "vitest";
import { fetchAuditRunPage } from "./audit";
import { collectAuditRun } from "../features/audit/export";
import type { AuditRunPage } from "../schemas/audit";

afterEach(() => vi.unstubAllGlobals());

const runId = "00000000-0000-4000-8000-000000000071";
const invoiceId = "00000000-0000-4000-8000-000000000072";
const event = {
  id: "00000000-0000-4000-8000-000000000073",
  run_id: runId,
  invoice_id: invoiceId,
  sequence: 1,
  event_type: "synthetic.audit",
  node: "Validate",
  actor_type: "POLICY",
  actor_id: "synthetic-policy",
  payload: { status: "PASS" },
  supersedes_id: null,
  versions: {
    graph_version: "graph@v1", model_version: "not-applicable@v1",
    prompt_version: "not-applicable@v1", policy_version: "policy@v1",
  },
  created_at: "2026-09-24T12:00:00Z",
} as const;
const page: AuditRunPage = {
  run_id: runId,
  invoice_id: invoiceId,
  trace_id: "a".repeat(32),
  status: "PAUSED",
  events: [event],
  next_cursor: null,
};

test("auditor history uses the role token and parses actor/version pins", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(page), {
    status: 200, headers: { "content-type": "application/json" },
  }));
  vi.stubGlobal("fetch", fetchMock);
  await expect(fetchAuditRunPage("synthetic-auditor-token", runId, 3)).resolves.toEqual(page);
  const request = fetchMock.mock.calls[0]?.[0] as Request;
  expect(request.headers.get("Authorization")).toBe("Bearer synthetic-auditor-token");
  expect(request.url).toContain("after_sequence=3");
  expect(request.url).toContain("limit=100");
});

test("export traverses every page and rejects a repeated cursor", async () => {
  const second = { ...page, events: [{ ...event, sequence: 2 }], next_cursor: null };
  const loadPage = vi.fn().mockResolvedValueOnce({
    ...page, next_cursor: { run_id: runId, sequence: 1 },
  }).mockResolvedValueOnce(second);
  const document = await collectAuditRun("synthetic-auditor-token", runId, loadPage);
  expect(document.events.map((item) => item.sequence)).toEqual([1, 2]);
  expect(loadPage.mock.calls.map((call) => call[2])).toEqual([null, 1]);
  const badLoader = vi.fn()
    .mockResolvedValueOnce({ ...page, next_cursor: { run_id: runId, sequence: 1 } })
    .mockResolvedValue({
      ...page, events: [{ ...event, sequence: 2 }],
      next_cursor: { run_id: runId, sequence: 1 },
    });
  await expect(collectAuditRun("synthetic-auditor-token", runId, badLoader)).rejects.toThrow(
    "did not advance",
  );
});

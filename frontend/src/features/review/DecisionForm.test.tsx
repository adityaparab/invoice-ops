import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { invoiceDetailSchema } from "../../schemas/invoice_read";
import { DecisionForm } from "./DecisionForm";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const invoiceId = "00000000-0000-4000-8000-000000000001";
const runId = "00000000-0000-4000-8000-000000000002";
const exceptionId = "00000000-0000-4000-8000-000000000003";
const proposalId = "00000000-0000-4000-8000-000000000004";

const detail = invoiceDetailSchema.parse({
  invoice: {
    id: invoiceId, run_id: runId, status: "NEEDS_REVIEW", run_status: "PAUSED",
    source: "UPLOAD", content_type: "application/pdf", vendor_name: "Synthetic Vendor",
    invoice_number: "SYN-1", po_number: "PO-1", currency: "USD", total_amount: "100",
    exception_id: exceptionId, exception_type: "PRICE_VARIANCE", exception_priority: 2,
    exception_sla_due_at: "2026-09-25T00:00:00Z", created_at: "2026-09-24T00:00:00Z",
  },
  exception: {
    id: exceptionId, exception_type: "PRICE_VARIANCE", status: "IN_REVIEW", priority: 2,
    sla_due_at: "2026-09-25T00:00:00Z", assigned_to: null, evidence: {},
    recommendation: {}, created_at: "2026-09-24T00:00:00Z",
  },
  pending_proposal: {
    id: proposalId, action: "RETURN", rationale: "Synthetic mismatch",
    reason_code: "AMOUNT_MISMATCH", actor_id: "maria-ap-analyst",
    created_at: "2026-09-24T01:00:00Z",
  },
  evidence: {}, read_at: "2026-09-24T12:00:00Z",
});

function installBrowserShims() {
  vi.stubGlobal("matchMedia", vi.fn().mockImplementation((query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })));
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
}

test("manager signoff binds to the analyst proposal and sends an idempotency key", async () => {
  installBrowserShims();
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
    decision_id: "00000000-0000-4000-8000-000000000005",
    exception_id: exceptionId,
    run_id: runId,
    invoice_id: invoiceId,
    action: "RETURN",
    actor_id: "dan-procurement-manager",
    stage: "QUEUED_FOR_RESUME",
    proposal_id: proposalId,
  }), { status: 201, headers: { "content-type": "application/json" } }));
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("crypto", { randomUUID: () => "00000000-0000-4000-8000-000000000006" });
  render(
    <MantineProvider>
      <QueryClientProvider client={new QueryClient()}>
        <DecisionForm detail={detail} token="synthetic-manager-token" persona="dan" />
      </QueryClientProvider>
    </MantineProvider>,
  );
  expect(screen.getByText("Synthetic mismatch")).toBeTruthy();
  fireEvent.change(screen.getByRole("textbox", { name: "Rationale" }), {
    target: { value: "Independent synthetic review confirms return" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Sign off decision" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  const request = fetchMock.mock.calls[0]?.[0] as Request;
  expect(request.headers.get("Idempotency-Key")).toBe("review-00000000-0000-4000-8000-000000000006");
  expect(request.headers.get("Authorization")).toBe("Bearer synthetic-manager-token");
  expect(await request.json()).toEqual({
    action: "RETURN", rationale: "Independent synthetic review confirms return",
    reason_code: "AMOUNT_MISMATCH", proposal_id: proposalId,
  });
  expect(await screen.findByText("The review worker will resume this run.")).toBeTruthy();
});

test("analyst proposal sends a new action without a manager proposal id", async () => {
  installBrowserShims();
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
    decision_id: "00000000-0000-4000-8000-000000000007",
    exception_id: exceptionId,
    run_id: runId,
    invoice_id: invoiceId,
    action: "ESCALATE",
    actor_id: "maria-ap-analyst",
    stage: "PENDING_SIGNOFF",
    proposal_id: null,
  }), { status: 201, headers: { "content-type": "application/json" } }));
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("crypto", { randomUUID: () => "00000000-0000-4000-8000-000000000006" });
  const analystDetail = invoiceDetailSchema.parse({
    ...detail,
    exception: { ...detail.exception, status: "OPEN" },
    pending_proposal: null,
  });
  render(
    <MantineProvider>
      <QueryClientProvider client={new QueryClient()}>
        <DecisionForm detail={analystDetail} token="synthetic-analyst-token" persona="maria" />
      </QueryClientProvider>
    </MantineProvider>,
  );
  fireEvent.change(screen.getByRole("textbox", { name: "Rationale" }), {
    target: { value: "Synthetic discrepancy needs escalation" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Submit proposal" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  const request = fetchMock.mock.calls[0]?.[0] as Request;
  expect(request.headers.get("Authorization")).toBe("Bearer synthetic-analyst-token");
  expect(await request.json()).toEqual({
    action: "ESCALATE", rationale: "Synthetic discrepancy needs escalation",
    reason_code: "MANUAL_REVIEW", proposal_id: null,
  });
  expect(await screen.findByText("A different manager must sign off.")).toBeTruthy();
});

import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { BrowserRouter } from "react-router";
import { afterEach, expect, test, vi } from "vitest";
import { invoiceSummarySchema } from "../../schemas/invoice_read";
import { ExceptionReview, slaLabel } from "./ExceptionReview";

vi.mock("../../app/persona", () => ({
  usePersona: () => ({ token: "synthetic-analyst-token", persona: "maria" }),
}));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const first = invoiceSummarySchema.parse({
  id: "00000000-0000-4000-8000-000000000001",
  run_id: "00000000-0000-4000-8000-000000000011",
  status: "NEEDS_REVIEW", run_status: "PAUSED", source: "UPLOAD",
  content_type: "application/pdf", vendor_name: "North Synthetic",
  invoice_number: "NORTH-1", po_number: "PO-NORTH", currency: "USD",
  total_amount: "100", exception_id: "00000000-0000-4000-8000-000000000021",
  exception_type: "PRICE_VARIANCE", exception_priority: 2,
  exception_sla_due_at: "2026-09-25T00:00:00Z", created_at: "2026-09-24T00:00:00Z",
});
const second = invoiceSummarySchema.parse({
  ...first,
  id: "00000000-0000-4000-8000-000000000002",
  run_id: "00000000-0000-4000-8000-000000000012",
  vendor_name: "South Synthetic", invoice_number: "SOUTH-2", po_number: "PO-SOUTH",
  exception_id: "00000000-0000-4000-8000-000000000022", exception_priority: 3,
});

test("queue loads another page, searches loaded rows, and sorts by vendor", async () => {
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
  const requests: URL[] = [];
  vi.stubGlobal("fetch", vi.fn().mockImplementation((request: Request) => {
    const url = new URL(request.url);
    requests.push(url);
    expect(request.headers.get("Authorization")).toBe("Bearer synthetic-analyst-token");
    const page = url.searchParams.get("cursor") === "next-page"
      ? { items: [second], next_cursor: null }
      : { items: [first], next_cursor: "next-page" };
    return Promise.resolve(new Response(JSON.stringify(page), {
      status: 200, headers: { "content-type": "application/json" },
    }));
  }));
  render(
    <MantineProvider>
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <BrowserRouter><ExceptionReview /></BrowserRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );

  expect(await screen.findByRole("button", { name: "NORTH-1" })).toBeTruthy();
  expect(requests[0]?.searchParams.get("exception_only")).toBe("true");
  fireEvent.click(screen.getByRole("button", { name: "Load more" }));
  expect(await screen.findByRole("button", { name: "SOUTH-2" })).toBeTruthy();
  expect(requests[1]?.searchParams.get("cursor")).toBe("next-page");

  const search = screen.getByRole("textbox", { name: "Search loaded rows" });
  fireEvent.change(search, { target: { value: "south" } });
  expect(screen.queryByRole("button", { name: "NORTH-1" })).toBeNull();
  expect(screen.getByRole("button", { name: "SOUTH-2" })).toBeTruthy();
  fireEvent.change(search, { target: { value: "" } });

  fireEvent.click(screen.getByRole("button", { name: "Vendor" }));
  const table = screen.getByRole("table");
  await waitFor(() => expect(within(table).getAllByRole("row")[1]?.textContent).toContain("North Synthetic"));
  fireEvent.click(screen.getByRole("button", { name: /Vendor/ }));
  expect(within(table).getAllByRole("row")[1]?.textContent).toContain("South Synthetic");
});

test("SLA labels distinguish overdue from upcoming deadlines at a pinned time", () => {
  const now = Date.parse("2026-09-24T12:00:00Z");
  expect(slaLabel("2026-09-24T10:00:00Z", now)).toBe("2h overdue");
  expect(slaLabel("2026-09-24T15:00:00Z", now)).toBe("Due in 3h");
});

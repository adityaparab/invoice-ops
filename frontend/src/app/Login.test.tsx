import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { BrowserRouter } from "react-router";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "./App";
import { PersonaProvider } from "./persona";

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  vi.unstubAllGlobals();
});

test("email/password login opens only the server-assigned role's workspace", async () => {
  vi.stubGlobal("matchMedia", vi.fn().mockImplementation((query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })));
  const requests: Request[] = [];
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request
      ? input : new Request(new URL(String(input), window.location.origin), init);
    requests.push(request);
    const path = new URL(request.url).pathname;
    const body = path === "/v1/auth/login" ? {
      email: "platform@example.test", role: "PLATFORM",
      token: `io_${"a".repeat(64)}`, expires_at: "2026-09-26T12:00:00Z",
    } : { status: "ok" };
    return Promise.resolve(new Response(JSON.stringify(body), {
      status: 200, headers: { "content-type": "application/json" },
    }));
  }));
  render(
    <MantineProvider>
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <BrowserRouter><PersonaProvider><App /></PersonaProvider></BrowserRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
  expect(screen.getByRole("heading", { name: "Sign in to InvoiceOps" })).toBeTruthy();
  fireEvent.change(screen.getByRole("textbox", { name: "Email" }), {
    target: { value: "platform@example.test" },
  });
  fireEvent.change(screen.getByLabelText(/Password/), {
    target: { value: "synthetic-password" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  await waitFor(() => expect(screen.getByText("Platform Engineer")).toBeTruthy());
  expect(screen.getByRole("navigation", { name: "Workspace" }).textContent).toContain("Evals");
  expect(screen.getByRole("navigation", { name: "Workspace" }).textContent).not.toContain("Dashboard");
  expect(screen.queryByLabelText("Persona API token")).toBeNull();
  const login = requests.find((request) => new URL(request.url).pathname === "/v1/auth/login");
  expect(login?.headers.get("Idempotency-Key")).toMatch(/^login-/);
});

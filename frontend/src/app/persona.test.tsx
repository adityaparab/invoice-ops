import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { PersonaProvider, usePersona } from "./persona";

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  vi.unstubAllGlobals();
});

function Harness() {
  const { persona, token, email, authenticated, signIn, signOut } = usePersona();
  return (
    <div>
      <output aria-label="persona">{persona}</output>
      <output aria-label="token">{token}</output>
      <output aria-label="email">{email}</output>
      <output aria-label="authenticated">{String(authenticated)}</output>
      <button onClick={() => { void signIn("dan@invoiceops.example", "test-password"); }}>Sign in</button>
      <button onClick={() => { void signOut(); }}>Sign out</button>
    </div>
  );
}

test("login selects the server role, saves this tab's session, and clears protected data on logout", async () => {
  const queryClient = new QueryClient();
  const token = `io_${"a".repeat(64)}`;
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
    email: "dan@invoiceops.example", role: "MANAGER", token,
    expires_at: "2026-09-26T12:00:00Z",
  }), { status: 200, headers: { "content-type": "application/json" } })));
  render(
    <QueryClientProvider client={queryClient}>
      <PersonaProvider><Harness /></PersonaProvider>
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  await waitFor(() => expect(screen.getByLabelText("persona").textContent).toBe("dan"));
  expect(screen.getByLabelText("token").textContent).toBe(token);
  expect(screen.getByLabelText("email").textContent).toBe("dan@invoiceops.example");
  expect(screen.getByLabelText("authenticated").textContent).toBe("true");
  expect(sessionStorage.getItem("invoiceops-session")).toContain(token);
  window.dispatchEvent(new CustomEvent("invoiceops:unauthorized", { detail: `io_${"b".repeat(64)}` }));
  expect(screen.getByLabelText("authenticated").textContent).toBe("true");
  queryClient.setQueryData(["invoice-page"], { privateInvoice: "synthetic" });
  fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
  expect(screen.getByLabelText("authenticated").textContent).toBe("false");
  expect(queryClient.getQueryData(["invoice-page"])).toBeUndefined();
  expect(sessionStorage.getItem("invoiceops-session")).toBeNull();
  expect(localStorage.length).toBe(0);
});

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { PersonaProvider, usePersona } from "./persona";

afterEach(cleanup);

function Harness() {
  const { persona, token, choosePersona, setToken } = usePersona();
  return (
    <div>
      <output aria-label="persona">{persona}</output>
      <output aria-label="token">{token}</output>
      <button onClick={() => setToken("synthetic-analyst-token")}>Set analyst token</button>
      <button onClick={() => choosePersona("dan")}>Switch to manager</button>
    </div>
  );
}

test("switching personas clears cached protected data and keeps tokens isolated", () => {
  const queryClient = new QueryClient();
  queryClient.setQueryData(["invoice-page", "maria"], { privateInvoice: "synthetic" });
  render(
    <QueryClientProvider client={queryClient}>
      <PersonaProvider><Harness /></PersonaProvider>
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Set analyst token" }));
  expect(screen.getByLabelText("token").textContent).toBe("synthetic-analyst-token");
  fireEvent.click(screen.getByRole("button", { name: "Switch to manager" }));
  expect(screen.getByLabelText("persona").textContent).toBe("dan");
  expect(screen.getByLabelText("token").textContent).toBe("");
  expect(queryClient.getQueryData(["invoice-page", "maria"])).toBeUndefined();
  expect(window.localStorage.length).toBe(0);
});

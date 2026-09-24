import { afterEach, expect, test, vi } from "vitest";
import { fetchHealth } from "./health";

afterEach(() => vi.unstubAllGlobals());

test("health response is parsed at the API boundary", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
    new Response(JSON.stringify({ status: "ok" }), {
      headers: { "content-type": "application/json" },
    }),
  ));
  await expect(fetchHealth()).resolves.toEqual({ status: "ok" });
});

test("invalid health wire data is rejected", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
    new Response(JSON.stringify({ status: "ok", extra: true }), {
      headers: { "content-type": "application/json" },
    }),
  ));
  await expect(fetchHealth()).rejects.toThrow();
});

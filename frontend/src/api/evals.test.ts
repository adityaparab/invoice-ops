import { afterEach, expect, test, vi } from "vitest";
import { fetchEvalDashboard } from "./evals";

afterEach(() => vi.unstubAllGlobals());

const report = {
  report_id: "extraction-baseline-v1",
  report_version: "extraction-baseline@v1",
  title: "Extraction development baseline",
  dataset_version: "voxel51-preparation-v1",
  measured_at: "2026-09-23T21:23:07Z",
  model_versions: ["gemini25flash"],
  metrics: [{
    key: "micro-f1", label: "Micro field F1", scope: "Tier A", value: "0.7226",
    unit: "rate", sample_count: 512, tp: 362, fp: 128, fn: 150,
  }],
  per_anomaly_confusion: [],
  tau_sweep: [],
  caveats: ["Development subset only."],
};
const dashboard = {
  reports: [report],
  experiments: [{
    id: "baseline-2026-09-23", report_id: report.report_id, date: "2026-09-23",
    hypothesis: "Synthetic hypothesis", change: "Baseline",
    observation: "Measured F1", decision: "Development only",
  }],
  read_at: "2026-09-24T12:00:00Z",
};

test("eval reports keep exact metric strings and use the auditor token", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(dashboard), {
    status: 200, headers: { "content-type": "application/json" },
  }));
  vi.stubGlobal("fetch", fetchMock);
  await expect(fetchEvalDashboard("synthetic-auditor-token")).resolves.toEqual(dashboard);
  const request = fetchMock.mock.calls[0]?.[0] as Request;
  expect(request.headers.get("Authorization")).toBe("Bearer synthetic-auditor-token");
  expect(request.url).toContain("/v1/evals/reports");
});

test("eval wire schema rejects numeric money or rates", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
    ...dashboard, reports: [{ ...report, metrics: [{ ...report.metrics[0], value: 0.7226 }] }],
  }), { status: 200, headers: { "content-type": "application/json" } })));
  await expect(fetchEvalDashboard("synthetic-auditor-token")).rejects.toThrow();
});

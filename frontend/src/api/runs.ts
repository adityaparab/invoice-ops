import { apiClient, requireData } from "./client";
import { runProgressSchema } from "../schemas/run_progress";

export async function fetchRunProgress(token: string, runId: string) {
  const { data, error, response } = await apiClient(token).GET("/v1/runs/{run_id}/progress", {
    params: { path: { run_id: runId } },
  });
  return runProgressSchema.parse(requireData(data, error, response.status));
}

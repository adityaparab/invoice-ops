import { apiClient, requireData } from "./client";
import { auditRunPageSchema } from "../schemas/audit";

export async function fetchAuditRunPage(
  token: string,
  runId: string,
  afterSequence: number | null = null,
) {
  const { data, error, response } = await apiClient(token).GET("/v1/runs/{run_id}/ledger", {
    params: {
      path: { run_id: runId },
      query: { limit: 100, after_sequence: afterSequence ?? undefined },
    },
  });
  return auditRunPageSchema.parse(requireData(data, error, response.status));
}

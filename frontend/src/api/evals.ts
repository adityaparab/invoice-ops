import { apiClient, requireData } from "./client";
import { evalDashboardSchema } from "../schemas/evals";

export async function fetchEvalDashboard(token: string) {
  const { data, error, response } = await apiClient(token).GET("/v1/evals/reports");
  return evalDashboardSchema.parse(requireData(data, error, response.status));
}

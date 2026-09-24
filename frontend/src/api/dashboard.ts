import { apiClient, requireData } from "./client";
import { dashboardSummarySchema } from "../schemas/dashboard";

export async function fetchDashboard(token: string, periodDays: number) {
  const { data, error, response } = await apiClient(token).GET("/v1/dashboard", {
    params: { query: { period_days: periodDays } },
  });
  return dashboardSummarySchema.parse(requireData(data, error, response.status));
}

import { apiClient, requireData } from "./client";
import { livenessResponseSchema } from "../schemas/health";

export async function fetchHealth() {
  const { data, error, response } = await apiClient().GET("/healthz");
  return livenessResponseSchema.parse(requireData(data, error, response.status));
}

import { apiClient, requireData } from "./client";
import { decisionRequestSchema, decisionResponseSchema } from "../schemas/decision";
import type { DecisionRequest } from "../schemas/decision";

export async function submitDecision(
  token: string,
  exceptionId: string,
  key: string,
  input: DecisionRequest,
) {
  const body = decisionRequestSchema.parse(input);
  const { data, error, response } = await apiClient(token).POST(
    "/v1/exceptions/{exception_id}/decision",
    {
      params: {
        path: { exception_id: exceptionId },
        header: { "Idempotency-Key": key },
      },
      body,
    },
  );
  return decisionResponseSchema.parse(requireData(data, error, response.status));
}

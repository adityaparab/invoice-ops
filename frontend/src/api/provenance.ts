import { apiClient, requireData } from "./client";
import { invoiceProvenancePageSchema, runTracePageSchema } from "../schemas/provenance";

export async function fetchRunTracePage(
  token: string, runId: string, afterSequence: number | null = null,
) {
  const { data, error, response } = await apiClient(token).GET("/v1/runs/{run_id}/trace", {
    params: {
      path: { run_id: runId },
      query: { limit: 100, after_sequence: afterSequence ?? undefined },
    },
  });
  return runTracePageSchema.parse(requireData(data, error, response.status));
}

export async function fetchInvoiceProvenancePage(
  token: string, invoiceId: string,
  cursor: { created_at: string; id: string } | null = null,
) {
  const { data, error, response } = await apiClient(token).GET(
    "/v1/invoices/{invoice_id}/provenance",
    {
      params: {
        path: { invoice_id: invoiceId },
        query: {
          limit: 100,
          after_created_at: cursor?.created_at,
          after_event_id: cursor?.id,
        },
      },
    },
  );
  return invoiceProvenancePageSchema.parse(requireData(data, error, response.status));
}

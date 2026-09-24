import { apiClient, requireData } from "./client";
import type { operations } from "../generated/api";
import { invoiceDetailSchema, invoicePageSchema } from "../schemas/invoice_read";

export type InvoiceFilters = NonNullable<
  operations["list_invoices_v1_invoices_get"]["parameters"]["query"]
>;

export async function fetchInvoicePage(token: string, filters: InvoiceFilters = {}) {
  const { data, error, response } = await apiClient(token).GET("/v1/invoices", {
    params: { query: filters },
  });
  return invoicePageSchema.parse(requireData(data, error, response.status));
}

export async function fetchInvoiceDetail(token: string, invoiceId: string) {
  const { data, error, response } = await apiClient(token).GET("/v1/invoices/{invoice_id}", {
    params: { path: { invoice_id: invoiceId } },
  });
  return invoiceDetailSchema.parse(requireData(data, error, response.status));
}

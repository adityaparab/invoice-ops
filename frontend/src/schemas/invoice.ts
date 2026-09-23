import { z } from "zod";

export const invoiceUploadResponseSchema = z
  .object({
    invoice_id: z.string().uuid(),
    run_id: z.string().uuid(),
    status: z.literal("QUEUED"),
    duplicate: z.boolean(),
  })
  .strict();

export type InvoiceUploadResponse = z.infer<typeof invoiceUploadResponseSchema>;

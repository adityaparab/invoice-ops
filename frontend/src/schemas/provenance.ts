import { z } from "zod";
import { invoiceLedgerCursorSchema, ledgerEventSchema, runLedgerCursorSchema } from "./ledger";
import { invoiceSourceSchema, invoiceStatusSchema, runStatusSchema } from "./invoice_read";

export const traceEventSchema = ledgerEventSchema.omit({
  payload: true, run_id: true, invoice_id: true,
});

export const runTracePageSchema = z.object({
  run_id: z.string().uuid(),
  invoice_id: z.string().uuid(),
  trace_id: z.string().regex(/^[0-9a-f]{32}$/),
  status: runStatusSchema,
  graph_version: z.string().min(1).max(128).regex(/\S/),
  started_at: z.string().datetime({ offset: true }).nullable(),
  completed_at: z.string().datetime({ offset: true }).nullable(),
  events: z.array(traceEventSchema).max(200),
  next_cursor: runLedgerCursorSchema.nullable(),
}).strict();

export const invoiceProvenancePageSchema = z.object({
  invoice_id: z.string().uuid(),
  status: invoiceStatusSchema,
  source: invoiceSourceSchema,
  created_at: z.string().datetime({ offset: true }),
  events: z.array(ledgerEventSchema).max(200),
  next_cursor: invoiceLedgerCursorSchema.nullable(),
}).strict();

export type RunTracePage = z.infer<typeof runTracePageSchema>;
export type InvoiceProvenancePage = z.infer<typeof invoiceProvenancePageSchema>;

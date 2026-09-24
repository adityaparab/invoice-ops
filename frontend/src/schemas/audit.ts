import { z } from "zod";
import { ledgerEventSchema, runLedgerCursorSchema } from "./ledger";
import { runStatusSchema } from "./invoice_read";

export const auditRunPageSchema = z.object({
  run_id: z.string().uuid(),
  invoice_id: z.string().uuid(),
  trace_id: z.string().regex(/^[0-9a-f]{32}$/),
  status: runStatusSchema,
  events: z.array(ledgerEventSchema).max(200),
  next_cursor: runLedgerCursorSchema.nullable(),
}).strict();

export type AuditRunPage = z.infer<typeof auditRunPageSchema>;

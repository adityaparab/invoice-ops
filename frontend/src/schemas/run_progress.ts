import { z } from "zod";
import { runStatusSchema } from "./invoice_read";
import { jsonValueSchema } from "./ledger";

export const invoiceNodeSchema = z.enum([
  "Ingest", "Extract", "Validate", "Match3Way", "Policy", "Gate",
  "AutoApprove", "ExceptionTriage", "HumanReview", "Archive", "Reject",
]);
const timestamp = z.string().datetime({ offset: true });

export const nodeProgressSchema = z.object({
  name: invoiceNodeSchema,
  observed_at: timestamp.nullable(),
  event_type: z.string().nullable(),
  state: z.record(z.string(), jsonValueSchema),
}).strict();

export const runProgressSchema = z.object({
  run_id: z.string().uuid(),
  invoice_id: z.string().uuid(),
  status: runStatusSchema,
  graph_version: z.string(),
  active_node: invoiceNodeSchema.nullable(),
  progress_source: z.literal("audit-ledger"),
  nodes: z.array(nodeProgressSchema).length(11),
  started_at: timestamp.nullable(),
  completed_at: timestamp.nullable(),
  read_at: timestamp,
}).strict();

export type RunProgress = z.infer<typeof runProgressSchema>;
export type NodeProgress = z.infer<typeof nodeProgressSchema>;

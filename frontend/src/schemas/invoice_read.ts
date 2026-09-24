import { z } from "zod";
import { jsonValueSchema } from "./ledger";

export const invoiceStatusSchema = z.enum([
  "RECEIVED", "QUEUED", "PROCESSING", "NEEDS_REVIEW", "APPROVED",
  "REJECTED", "RETURNED", "ARCHIVED", "FAILED",
]);
export const runStatusSchema = z.enum([
  "QUEUED", "RUNNING", "PAUSED", "COMPLETED", "FAILED", "CANCELLED",
]);
export const exceptionStatusSchema = z.enum(["OPEN", "IN_REVIEW", "RESOLVED", "ESCALATED"]);
export const invoiceSourceSchema = z.enum(["UPLOAD", "EMAIL"]);
const timestamp = z.string().datetime({ offset: true });
const jsonObject = z.record(z.string(), jsonValueSchema);

export const invoiceExceptionSchema = z.object({
  id: z.string().uuid(),
  exception_type: z.string().min(1).max(128),
  status: exceptionStatusSchema,
  priority: z.number().int().min(0).max(3),
  sla_due_at: timestamp,
  assigned_to: z.string().max(128).nullable(),
  evidence: jsonObject,
  recommendation: jsonObject,
  created_at: timestamp,
}).strict();

export const invoiceSummarySchema = z.object({
  id: z.string().uuid(),
  run_id: z.string().uuid(),
  status: invoiceStatusSchema,
  run_status: runStatusSchema,
  source: invoiceSourceSchema,
  content_type: z.enum(["application/pdf", "image/png", "image/jpeg"]),
  vendor_name: z.string().max(256).nullable(),
  invoice_number: z.string().max(128).nullable(),
  po_number: z.string().max(128).nullable(),
  currency: z.string().regex(/^[A-Z]{3}$/).nullable(),
  total_amount: z.string().regex(/^-?(?:0|[1-9]\d*)(?:\.\d+)?$/).nullable(),
  exception_id: z.string().uuid().nullable(),
  exception_type: z.string().max(128).nullable(),
  exception_priority: z.number().int().min(0).max(3).nullable(),
  exception_sla_due_at: timestamp.nullable(),
  created_at: timestamp,
}).strict();

export const invoicePageSchema = z.object({
  items: z.array(invoiceSummarySchema).max(100),
  next_cursor: z.string().max(200).nullable(),
}).strict();

export const pendingProposalSchema = z.object({
  id: z.string().uuid(),
  action: z.enum(["APPROVE", "RETURN", "ESCALATE"]),
  rationale: z.string(),
  reason_code: z.string(),
  actor_id: z.string(),
  created_at: timestamp,
}).strict();

export const invoiceDetailSchema = z.object({
  invoice: invoiceSummarySchema,
  exception: invoiceExceptionSchema.nullable(),
  pending_proposal: pendingProposalSchema.nullable(),
  evidence: z.record(z.string(), jsonObject),
  read_at: timestamp,
}).strict();

export type InvoiceSummary = z.infer<typeof invoiceSummarySchema>;
export type InvoicePage = z.infer<typeof invoicePageSchema>;
export type InvoiceDetail = z.infer<typeof invoiceDetailSchema>;

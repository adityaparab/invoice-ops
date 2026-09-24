import { z } from "zod";

const nonnegativeDecimal = z.string().regex(/^(?:0|[1-9]\d*)(?:\.\d+)?$/);
const count = z.number().int().nonnegative();

export const dashboardSummarySchema = z.object({
  as_of: z.string().datetime({ offset: true }),
  period_days: z.number().int().min(1).max(90),
  invoice_count: count,
  resolved_count: count,
  auto_approved_count: count,
  stp_rate: nonnegativeDecimal.nullable(),
  open_exception_count: count,
  aging: z.object({
    under_24_hours: count,
    one_to_three_days: count,
    over_three_days: count,
    sla_overdue: count,
  }).strict(),
  cost_observed_invoices: count,
  total_observed_cost_usd: nonnegativeDecimal.nullable(),
  cost_per_observed_invoice_usd: nonnegativeDecimal.nullable(),
  cost_coverage: z.enum(["COMPLETE", "PARTIAL", "UNAVAILABLE"]),
  volume_by_day: z.array(z.object({ day: z.string().date(), invoices: count }).strict()).max(90),
  exception_types: z.array(z.object({ code: z.string().min(1).max(128), count }).strict()),
}).strict();

export type DashboardSummary = z.infer<typeof dashboardSummarySchema>;

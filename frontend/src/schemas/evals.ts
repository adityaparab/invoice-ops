import { z } from "zod";

const decimal = z.string().regex(/^-?(?:0|[1-9]\d*)(?:\.\d+)?$/);
const timestamp = z.string().datetime({ offset: true });

export const metricRowSchema = z.object({
  key: z.string().min(1).max(128),
  label: z.string().min(1).max(128),
  scope: z.string().min(1).max(128),
  value: decimal,
  unit: z.enum(["rate", "usd", "ms", "count"]),
  sample_count: z.number().int().nonnegative().nullable(),
  tp: z.number().int().nonnegative().nullable(),
  fp: z.number().int().nonnegative().nullable(),
  fn: z.number().int().nonnegative().nullable(),
}).strict();

export const anomalyConfusionSchema = z.object({
  anomaly_code: z.string().min(1).max(128),
  tp: z.number().int().nonnegative(),
  fp: z.number().int().nonnegative(),
  fn: z.number().int().nonnegative(),
  tn: z.number().int().nonnegative(),
}).strict();

export const tauSweepPointSchema = z.object({
  threshold: decimal,
  exception_recall: decimal,
  false_escalation_rate: decimal,
  stp_rate: decimal,
}).strict();

export const evalReportSchema = z.object({
  report_id: z.string().regex(/^[a-z0-9][a-z0-9-]{1,99}$/),
  report_version: z.string().min(1).max(128),
  title: z.string().min(1).max(200),
  dataset_version: z.string().min(1).max(128),
  measured_at: timestamp,
  model_versions: z.array(z.string()).max(20),
  metrics: z.array(metricRowSchema).max(500),
  per_anomaly_confusion: z.array(anomalyConfusionSchema).max(100),
  tau_sweep: z.array(tauSweepPointSchema).max(101),
  caveats: z.array(z.string()).max(20),
}).strict();

export const experimentEntrySchema = z.object({
  id: z.string().regex(/^[a-z0-9][a-z0-9-]{1,99}$/),
  report_id: z.string().regex(/^[a-z0-9][a-z0-9-]{1,99}$/),
  date: z.iso.date(),
  hypothesis: z.string().min(1).max(1000),
  change: z.string().min(1).max(1000),
  observation: z.string().min(1).max(1000),
  decision: z.string().min(1).max(1000),
}).strict();

export const evalDashboardSchema = z.object({
  reports: z.array(evalReportSchema).max(50),
  experiments: z.array(experimentEntrySchema).max(200),
  read_at: timestamp,
}).strict();

export type EvalDashboard = z.infer<typeof evalDashboardSchema>;
export type EvalReport = z.infer<typeof evalReportSchema>;
export type MetricRow = z.infer<typeof metricRowSchema>;

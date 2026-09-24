import { z } from "zod";

export const triageFactSchema = z.object({
  ref: z.string().max(100).regex(/^[a-z_]+:[A-Za-z0-9_:.-]+$/),
  detail: z.string().min(1).max(500),
}).strict();

export const triageEvidenceSchema = z.object({
  version: z.literal("triage-evidence@v1"),
  facts: z.array(triageFactSchema).min(1).max(150),
}).strict();

export const triageRequestSchema = z.object({
  run_id: z.string().uuid(),
  invoice_id: z.string().uuid(),
  trace_id: z.string().regex(/^[0-9a-f]{32}$/),
  evidence: triageEvidenceSchema,
  evidence_sha256: z.string().regex(/^[0-9a-f]{64}$/),
}).strict();

export const triageDraftSchema = z.object({
  recommended_action: z.enum(["APPROVE", "RETURN", "ESCALATE"]),
  summary: z.string().min(1).max(500),
  rationale: z.string().min(1).max(2000),
  evidence_refs: z.array(z.string()).min(1).max(20),
}).strict();

export const triageResultSchema = z.object({
  status: z.enum(["DRAFT", "FALLBACK"]),
  draft: triageDraftSchema.nullable(),
  fallback_reason: z.enum([
    "GATEWAY_FAILURE", "INVALID_EVIDENCE_REFS", "POLICY_CONFLICT",
  ]).nullable(),
  evidence_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  model_version: z.string().min(1).max(160),
  prompt_version: z.literal("triage@v1"),
  gateway_attempts: z.number().int().min(0).max(6),
  input_tokens: z.number().int().min(0).nullable(),
  output_tokens: z.number().int().min(0).nullable(),
  latency_ms: z.number().min(0).nullable(),
  cost_usd: z.string().regex(/^(?:0|[1-9]\d*)(?:\.\d+)?$/).nullable(),
}).strict();

export type TriageResult = z.infer<typeof triageResultSchema>;

import { z } from "zod";

export const decisionActionSchema = z.enum(["APPROVE", "RETURN", "ESCALATE"]);
export const decisionRequestSchema = z.object({
  action: decisionActionSchema,
  rationale: z.string().trim().min(1).max(2000),
  reason_code: z.string().regex(/^[A-Z][A-Z0-9_]{1,63}$/),
  proposal_id: z.string().uuid().nullable(),
}).strict();
export const decisionResponseSchema = z.object({
  decision_id: z.string().uuid(),
  exception_id: z.string().uuid(),
  run_id: z.string().uuid(),
  invoice_id: z.string().uuid(),
  action: decisionActionSchema,
  actor_id: z.string().min(1),
  stage: z.enum(["PENDING_SIGNOFF", "QUEUED_FOR_RESUME"]),
  proposal_id: z.string().uuid().nullable(),
}).strict();

export type DecisionRequest = z.infer<typeof decisionRequestSchema>;
export type DecisionResponse = z.infer<typeof decisionResponseSchema>;

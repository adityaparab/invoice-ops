import { z } from "zod";

type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };
const jsonValueSchema: z.ZodType<JsonValue> = z.lazy(() =>
  z.union([
    z.string(),
    z.number().finite(),
    z.boolean(),
    z.null(),
    z.array(jsonValueSchema),
    z.record(z.string(), jsonValueSchema),
  ]),
);
const nonblank = z.string().min(1).max(128).regex(/\S/);

export const versionPinsSchema = z
  .object({
    graph_version: nonblank,
    model_version: nonblank,
    prompt_version: nonblank,
    policy_version: nonblank,
  })
  .strict();

export const versionOverridesSchema = z
  .object({
    graph_version: nonblank.nullable().optional(),
    model_version: nonblank.nullable().optional(),
    prompt_version: nonblank.nullable().optional(),
    policy_version: nonblank.nullable().optional(),
  })
  .strict();

export const appendEventSchema = z
  .object({
    run_id: z.string().uuid(),
    invoice_id: z.string().uuid(),
    event_type: nonblank,
    actor_type: z.enum(["SYSTEM", "AGENT", "HUMAN", "POLICY"]),
    actor_id: nonblank,
    payload: z.record(z.string(), jsonValueSchema),
    node: nonblank.nullable().optional(),
    supersedes_id: z.string().uuid().nullable().optional(),
    versions: versionOverridesSchema.nullable().optional(),
  })
  .strict();

export const ledgerEventSchema = appendEventSchema
  .omit({ versions: true, node: true, supersedes_id: true })
  .extend({
    id: z.string().uuid(),
    sequence: z.number().int().min(1),
    node: nonblank.nullable(),
    supersedes_id: z.string().uuid().nullable(),
    versions: versionPinsSchema,
    created_at: z.string().datetime({ offset: true }),
  });

export const runLedgerCursorSchema = z
  .object({ run_id: z.string().uuid(), sequence: z.number().int().min(1) })
  .strict();
export const invoiceLedgerCursorSchema = z
  .object({
    invoice_id: z.string().uuid(),
    created_at: z.string().datetime({ offset: true }),
    id: z.string().uuid(),
  })
  .strict();
export const runLedgerPageSchema = z
  .object({
    events: z.array(ledgerEventSchema).max(200),
    next_cursor: runLedgerCursorSchema.nullable(),
  })
  .strict();
export const invoiceLedgerPageSchema = z
  .object({
    events: z.array(ledgerEventSchema).max(200),
    next_cursor: invoiceLedgerCursorSchema.nullable(),
  })
  .strict();

export type VersionPins = z.infer<typeof versionPinsSchema>;
export type AppendEvent = z.infer<typeof appendEventSchema>;
export type LedgerEvent = z.infer<typeof ledgerEventSchema>;
export type RunLedgerPage = z.infer<typeof runLedgerPageSchema>;
export type InvoiceLedgerPage = z.infer<typeof invoiceLedgerPageSchema>;

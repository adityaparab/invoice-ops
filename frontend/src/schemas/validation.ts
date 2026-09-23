import { z } from "zod";
import { invoiceExtractionSchema } from "./extraction";

// Decimal evidence is fixed-point text; never coerce monetary values to JS numbers.
const decimal = z.string().regex(/^-?(?:0|[1-9]\d*)(?:\.\d+)?$/);
const nonnegativeDecimal = z.string().regex(/^(?:0|[1-9]\d*)(?:\.\d+)?$/);
const checksum = z.string().regex(/^[0-9a-f]{64}$/);

export const currencyRuleSchema = z.object({
  currency: z.string().regex(/^[A-Z]{3}$/),
  decimal_places: z.number().int().min(0).max(4),
  absolute_tolerance: z.string().regex(/^(?:(?:0|[1-9]\d?)(?:\.\d{1,4})?|100(?:\.0{1,4})?)$/),
}).strict();

export const validationConfigSchema = z.object({
  version: z.string().regex(/^[A-Za-z0-9@_.:-]{1,128}$/),
  rounding: z.literal("ROUND_HALF_UP"),
  tax_method: z.literal("sum-rounded-line-taxes"),
  currencies: z.array(currencyRuleSchema).min(1).max(200),
}).strict().refine(
  (config) => new Set(config.currencies.map((rule) => rule.currency)).size === config.currencies.length,
  { message: "Currency codes must be unique", path: ["currencies"] },
);

export const validationIssueSchema = z.object({
  code: z.enum([
    "REQUIRED_FIELD", "EMPTY_LINES", "NEGATIVE_VALUE", "NONPOSITIVE_QUANTITY",
    "TAX_RATE_OUT_OF_RANGE", "UNSUPPORTED_CURRENCY", "LINE_MATH_MISMATCH",
    "SUBTOTAL_MISMATCH", "TAX_MISMATCH", "TOTAL_MISMATCH",
  ]),
  field: z.string().regex(/^[a-z_]+(?:\.[a-z_]+)?$/),
  line_number: z.number().int().min(1).max(500).nullable(),
  expected: decimal.nullable(),
  actual: decimal.nullable(),
  difference: decimal.nullable(),
  tolerance: nonnegativeDecimal.nullable(),
}).strict();

export const validationResultSchema = z.object({
  status: z.enum(["PASS", "FAIL"]),
  input_sha256: checksum,
  config_sha256: checksum,
  config: validationConfigSchema,
  issues: z.array(validationIssueSchema),
}).strict().refine(
  (result) => (result.status === "PASS") === (result.issues.length === 0),
  { message: "PASS requires no issues; FAIL requires an issue", path: ["status"] },
);

export const validationRequestSchema = z.object({
  run_id: z.string().uuid(),
  invoice_id: z.string().uuid(),
  trace_id: z.string().regex(/^[0-9a-f]{32}$/),
  extraction: invoiceExtractionSchema,
}).strict();

export type ValidationConfig = z.infer<typeof validationConfigSchema>;
export type ValidationIssue = z.infer<typeof validationIssueSchema>;
export type ValidationResult = z.infer<typeof validationResultSchema>;
export type ValidationRequest = z.infer<typeof validationRequestSchema>;

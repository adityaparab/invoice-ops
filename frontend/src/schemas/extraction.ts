import { z } from "zod";

// The Python boundary serializes finite Decimals as canonical base-10 strings.
const confidenceSchema = z.string().regex(/^(?:0(?:\.\d{1,6})?|1(?:\.0{1,6})?)$/);
const decimalSchema = (wholeDigits: number, fractionalDigits: number) =>
  z.string().regex(
    new RegExp(`^-?(?:0|[1-9]\\d{0,${wholeDigits - 1}})(?:\\.\\d{1,${fractionalDigits}})?$`),
  );
const amountSchema = decimalSchema(14, 4);
const quantitySchema = decimalSchema(12, 6);
const taxRateSchema = decimalSchema(6, 6);

const extractedField = <T extends z.ZodType>(valueSchema: T) =>
  z.object({ value: valueSchema.nullable(), confidence: confidenceSchema })
    .strict()
    .refine((field) => "value" in field && (field.value !== null || /^0(?:\.0+)?$/.test(field.confidence)), {
      message: "Unknown values must have zero confidence",
      path: ["confidence"],
    });

export const invoiceLineItemSchema = z.object({
  description: extractedField(z.string().min(1).max(1000)),
  quantity: extractedField(quantitySchema),
  unit_price: extractedField(amountSchema),
  tax_rate: extractedField(taxRateSchema), // Fraction: 0.20 means 20%.
  line_total: extractedField(amountSchema), // Net, excluding tax.
}).strict();

export const invoiceExtractionSchema = z.object({
  vendor_name: extractedField(z.string().min(1).max(256)),
  vendor_tax_id: extractedField(z.string().min(1).max(128)),
  bank_account_iban: extractedField(z.string().min(1).max(64)),
  invoice_number: extractedField(z.string().min(1).max(128)),
  po_number: extractedField(z.string().min(1).max(128)),
  currency: extractedField(z.string().regex(/^[A-Z]{3}$/)),
  invoice_date: extractedField(z.string().date()),
  due_date: extractedField(z.string().date()),
  subtotal: extractedField(amountSchema), // Net, excluding tax.
  tax_amount: extractedField(amountSchema),
  total_amount: extractedField(amountSchema), // Gross, including tax.
  line_items: z.array(invoiceLineItemSchema).max(500),
}).strict();

export type InvoiceExtraction = z.infer<typeof invoiceExtractionSchema>;

export const extractionRequestSchema = z.object({
  run_id: z.string().uuid(),
  invoice_id: z.string().uuid(),
  trace_id: z.string().regex(/^[0-9a-f]{32}$/),
  raw_ref: z.string().min(1).max(256),
  content_hash: z.string().regex(/^[a-f0-9]{64}$/),
  content_type: z.enum(["application/pdf", "image/png", "image/jpeg"]),
  scenario: z.string().regex(/^[A-Za-z0-9_\-]{1,64}$/).default("extraction"),
}).strict();

const modelUsageSchema = z.object({
  input_tokens: z.number().int().nonnegative(),
  output_tokens: z.number().int().nonnegative(),
  total_tokens: z.number().int().nonnegative(),
}).strict().refine((usage) => usage.total_tokens === usage.input_tokens + usage.output_tokens);

export const modelInvocationSchema = z.object({
  prompt_version: z.string().min(1).max(128),
  model_version: z.string().min(1).max(128),
  provider_model: z.string().max(160).nullable().default(null),
  status: z.enum(["VALID", "MALFORMED", "FAILED"]),
  gateway_attempts: z.number().int().min(0).max(6),
  usage: modelUsageSchema.nullable().default(null),
  latency_ms: z.number().finite().nonnegative().nullable().default(null),
  cost_usd: z.string().regex(/^(?:\+?\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?|-0(?:\.0+)?(?:[Ee][+-]?\d+)?)$/).nullable().default(null),
}).strict();

const extractionOutcomeShape = {
  run_id: z.string().uuid(),
  invoice_id: z.string().uuid(),
  trace_id: z.string().regex(/^[0-9a-f]{32}$/),
  calls: z.array(modelInvocationSchema).max(2),
};

export const extractionResultSchema = z.discriminatedUnion("status", [
  z.object({ ...extractionOutcomeShape, status: z.literal("EXTRACTED"), extraction: invoiceExtractionSchema }).strict(),
  z.object({
    ...extractionOutcomeShape,
    status: z.literal("ESCALATED"),
    reason: z.enum([
      "MALFORMED_MODEL_OUTPUT", "INVALID_MODEL_RESPONSE", "GATEWAY_UNAVAILABLE",
      "GATEWAY_REJECTED", "GUARDRAIL_REJECTED", "TOKEN_BUDGET_EXCEEDED",
      "DOCUMENT_UNAVAILABLE", "INVALID_DOCUMENT", "UNSUPPORTED_DOCUMENT",
    ]),
  }).strict(),
]);

export type ExtractionRequest = z.infer<typeof extractionRequestSchema>;
export type ExtractionResult = z.infer<typeof extractionResultSchema>;

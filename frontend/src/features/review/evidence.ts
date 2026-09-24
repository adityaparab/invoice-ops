import { z } from "zod";
import { extractionResultSchema } from "../../schemas/extraction";
import { triageEvidenceSchema, triageResultSchema } from "../../schemas/triage";
import type { InvoiceDetail } from "../../schemas/invoice_read";

const checkStatus = z.enum(["MATCH", "MISMATCH", "UNKNOWN"]);
const identityCheck = z.object({
  field: z.string(),
  line_number: z.number().int().positive().nullable(),
  expected: z.string().nullable(),
  actual: z.string().nullable(),
  status: checkStatus,
}).passthrough();
const numericCheck = z.object({
  field: z.string(),
  line_number: z.number().int().positive().nullable(),
  expected: z.string().nullable(),
  actual: z.string().nullable(),
  status: checkStatus,
}).passthrough();
const matchResult = z.object({
  status: z.enum(["PASS", "FAIL", "INCOMPLETE"]),
  snapshot_found: z.boolean(),
  po_number: z.string().nullable(),
  po_status: z.string().nullable(),
  identity_checks: z.array(identityCheck),
  numeric_checks: z.array(numericCheck),
}).passthrough();

export function readReviewEvidence(detail: InvoiceDetail) {
  const extraction = extractionResultSchema.safeParse(detail.evidence["extraction.completed"]?.result);
  const match = matchResult.safeParse(detail.evidence["matching.completed"]);
  const triage = triageResultSchema.safeParse(detail.exception?.recommendation.triage);
  const facts = triageEvidenceSchema.safeParse(detail.exception?.recommendation.evidence);
  return {
    extraction: extraction.success && extraction.data.status === "EXTRACTED"
      ? extraction.data.extraction : null,
    match: match.success ? match.data : null,
    triage: triage.success ? triage.data : null,
    facts: facts.success ? facts.data.facts : [],
  };
}

export type ReviewEvidence = ReturnType<typeof readReviewEvidence>;

export interface ThreeWayRow {
  label: string;
  invoice: string;
  purchaseOrder: string;
  goodsReceipt: string;
  status: "MATCH" | "MISMATCH" | "UNKNOWN";
}

function text(value: string | null | undefined): string {
  return value ?? "—";
}

export function threeWayRows(evidence: ReviewEvidence): ThreeWayRow[] {
  const match = evidence.match;
  if (!match) return [];
  const rows: ThreeWayRow[] = match.identity_checks
    .filter((check) => check.line_number === null)
    .map((check) => ({
      label: check.field.replaceAll("_", " "),
      invoice: text(check.actual),
      purchaseOrder: text(check.expected),
      goodsReceipt: "—",
      status: check.status,
    }));
  const ordered = new Map(match.numeric_checks.map((check) => [`${check.line_number}:${check.field}`, check]));
  for (const check of match.numeric_checks) {
    if (check.field === "quantity_ordered") {
      const receipt = ordered.get(`${check.line_number}:quantity_received`);
      rows.push({
        label: `Line ${check.line_number} quantity`,
        invoice: text(check.actual),
        purchaseOrder: text(check.expected),
        goodsReceipt: text(receipt?.expected),
        status: check.status === "MISMATCH" || receipt?.status === "MISMATCH" ? "MISMATCH"
          : check.status === "UNKNOWN" || receipt?.status === "UNKNOWN" ? "UNKNOWN" : "MATCH",
      });
    } else if (check.field === "subtotal_ordered") {
      const receipt = ordered.get("null:subtotal_received");
      rows.push({
        label: "Subtotal",
        invoice: text(check.actual),
        purchaseOrder: text(check.expected),
        goodsReceipt: text(receipt?.expected),
        status: check.status === "MISMATCH" || receipt?.status === "MISMATCH" ? "MISMATCH"
          : check.status === "UNKNOWN" || receipt?.status === "UNKNOWN" ? "UNKNOWN" : "MATCH",
      });
    } else if (check.field === "unit_price" || check.field === "line_total") {
      rows.push({
        label: `Line ${check.line_number} ${check.field.replaceAll("_", " ")}`,
        invoice: text(check.actual),
        purchaseOrder: text(check.expected),
        goodsReceipt: "—",
        status: check.status,
      });
    }
  }
  return rows;
}

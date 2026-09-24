import { describe, expect, it } from "vitest";
import { invoiceDetailSchema } from "../../schemas/invoice_read";
import { readReviewEvidence, threeWayRows } from "./evidence";
import { slaLabel } from "./ExceptionReview";

const invoiceId = "00000000-0000-4000-8000-000000000001";
const runId = "00000000-0000-4000-8000-000000000002";
const exceptionId = "00000000-0000-4000-8000-000000000003";

function detail(evidence: Record<string, Record<string, unknown>>) {
  return invoiceDetailSchema.parse({
    invoice: {
      id: invoiceId, run_id: runId, status: "NEEDS_REVIEW", run_status: "PAUSED",
      source: "UPLOAD", content_type: "application/pdf", vendor_name: "Synthetic Vendor",
      invoice_number: "SYN-1", po_number: "PO-1", currency: "USD", total_amount: "100",
      exception_id: exceptionId, exception_type: "PRICE_VARIANCE", exception_priority: 2,
      exception_sla_due_at: "2026-09-25T00:00:00Z", created_at: "2026-09-24T00:00:00Z",
    },
    exception: {
      id: exceptionId, exception_type: "PRICE_VARIANCE", status: "OPEN", priority: 2,
      sla_due_at: "2026-09-25T00:00:00Z", assigned_to: null, evidence: {},
      recommendation: {}, created_at: "2026-09-24T00:00:00Z",
    },
    pending_proposal: null,
    evidence,
    read_at: "2026-09-24T12:00:00Z",
  });
}

describe("review evidence", () => {
  it("maps invoice, PO, and receipt quantities without hiding a mismatch", () => {
    const parsed = readReviewEvidence(detail({
      "matching.completed": {
        status: "FAIL", snapshot_found: true, po_number: "PO-1", po_status: "OPEN",
        identity_checks: [],
        numeric_checks: [
          { field: "quantity_ordered", line_number: 1, expected: "10", actual: "8", status: "MATCH" },
          { field: "quantity_received", line_number: 1, expected: "7", actual: "8", status: "MISMATCH" },
        ],
      },
    }));
    expect(threeWayRows(parsed)).toContainEqual({
      label: "Line 1 quantity", invoice: "8", purchaseOrder: "10",
      goodsReceipt: "7", status: "MISMATCH",
    });
  });

  it("treats malformed optional match evidence as unavailable", () => {
    expect(threeWayRows(readReviewEvidence(detail({ "matching.completed": { status: "PASS" } })))).toEqual([]);
  });

  it("shows SLA aging at the boundary", () => {
    const now = Date.parse("2026-09-24T12:00:00Z");
    expect(slaLabel("2026-09-24T14:00:00Z", now)).toBe("Due in 2h");
    expect(slaLabel("2026-09-24T10:00:00Z", now)).toBe("2h overdue");
  });
});

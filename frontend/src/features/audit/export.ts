import { fetchAuditRunPage } from "../../api/audit";
import type { AuditRunPage } from "../../schemas/audit";

export async function collectAuditRun(
  token: string,
  runId: string,
  loadPage: typeof fetchAuditRunPage = fetchAuditRunPage,
) {
  const events: AuditRunPage["events"] = [];
  const seen = new Set<number>();
  let after: number | null = null;
  let first: AuditRunPage | null = null;
  while (true) {
    const page = await loadPage(token, runId, after);
    if (page.run_id !== runId || (first && (
      page.invoice_id !== first.invoice_id || page.trace_id !== first.trace_id
    ))) {
      throw new Error("Audit history changed scope during export.");
    }
    let previous = after ?? 0;
    for (const event of page.events) {
      if (event.run_id !== runId || event.invoice_id !== page.invoice_id || event.sequence <= previous) {
        throw new Error("Audit event sequence or scope is invalid.");
      }
      previous = event.sequence;
    }
    first ??= page;
    events.push(...page.events);
    if (page.next_cursor === null) break;
    const next = page.next_cursor.sequence;
    if (page.next_cursor.run_id !== runId || seen.has(next) || next !== page.events.at(-1)?.sequence) {
      throw new Error("Audit pagination did not advance.");
    }
    seen.add(next);
    after = next;
  }
  if (first === null) throw new Error("Audit history was not returned.");
  return {
    run_id: first.run_id,
    invoice_id: first.invoice_id,
    trace_id: first.trace_id,
    status: first.status,
    exported_at: new Date().toISOString(),
    events,
  };
}

export function downloadAuditRun(runId: string, document: Awaited<ReturnType<typeof collectAuditRun>>) {
  const blob = new Blob([JSON.stringify(document, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = window.document.createElement("a");
  anchor.href = url;
  anchor.download = `invoiceops-run-${runId}-provenance.json`;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

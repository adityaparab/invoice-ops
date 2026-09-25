import { expect, test } from "@playwright/test";

const invoiceId = "00000000-0000-4000-8000-000000000001";
const runId = "00000000-0000-4000-8000-000000000002";
const exceptionId = "00000000-0000-4000-8000-000000000003";
const proposalId = "00000000-0000-4000-8000-000000000004";

const invoice = {
  id: invoiceId, run_id: runId, status: "NEEDS_REVIEW", run_status: "PAUSED",
  source: "UPLOAD", content_type: "application/pdf", vendor_name: "Synthetic Vendor",
  invoice_number: "SYN-1", po_number: "PO-1", currency: "USD", total_amount: "100",
  exception_id: exceptionId, exception_type: "PRICE_VARIANCE", exception_priority: 2,
  exception_sla_due_at: "2026-09-25T00:00:00Z", created_at: "2026-09-24T00:00:00Z",
};

test("analyst proposes and an independent manager signs off", async ({ page }) => {
  let proposed = false;
  const decisions: Array<{ token: string | null; action: string; proposal_id: string | null }> = [];
  const analystToken = `io_${"a".repeat(64)}`;
  const managerToken = `io_${"b".repeat(64)}`;

  await page.route("**/healthz", (route) => route.fulfill({ json: { status: "ok" } }));
  await page.route("**/v1/auth/login", async (route) => {
    const body = route.request().postDataJSON() as { email: string; password: string };
    expect(route.request().headers()["idempotency-key"]).toMatch(/^login-/);
    const manager = body.email === "manager@example.test";
    expect(body.password).toBe(manager ? "synthetic-manager-password" : "synthetic-analyst-password");
    await route.fulfill({ json: {
      email: body.email, role: manager ? "MANAGER" : "ANALYST",
      token: manager ? managerToken : analystToken, expires_at: "2026-09-26T12:00:00Z",
    } });
  });
  await page.route("**/v1/auth/logout", (route) => route.fulfill({ json: { status: "signed_out" } }));
  await page.route("**/v1/invoices?**", (route) => route.fulfill({
    json: { items: [invoice], next_cursor: null },
  }));
  await page.route(`**/v1/invoices/${invoiceId}`, (route) => route.fulfill({ json: {
    invoice,
    exception: {
      id: exceptionId, exception_type: "PRICE_VARIANCE", status: proposed ? "IN_REVIEW" : "OPEN",
      priority: 2, sla_due_at: "2026-09-25T00:00:00Z", assigned_to: null,
      evidence: {}, recommendation: {}, created_at: "2026-09-24T00:00:00Z",
    },
    pending_proposal: proposed ? {
      id: proposalId, action: "ESCALATE", rationale: "Synthetic discrepancy needs escalation",
      reason_code: "MANUAL_REVIEW", actor_id: "maria-ap-analyst",
      created_at: "2026-09-24T01:00:00Z",
    } : null,
    evidence: {}, read_at: "2026-09-24T12:00:00Z",
  } }));
  await page.route(`**/v1/exceptions/${exceptionId}/decision`, async (route) => {
    const request = route.request();
    const body = request.postDataJSON() as { action: string; proposal_id: string | null };
    const token = request.headers()["authorization"] ?? null;
    expect(request.headers()["idempotency-key"]).toMatch(/^review-[\w-]+$/);
    decisions.push({ token, action: body.action, proposal_id: body.proposal_id });
    const stage = proposed ? "QUEUED_FOR_RESUME" : "PENDING_SIGNOFF";
    if (!proposed) proposed = true;
    await route.fulfill({ status: 201, json: {
      decision_id: "00000000-0000-4000-8000-000000000005",
      exception_id: exceptionId, run_id: runId, invoice_id: invoiceId,
      action: body.action, actor_id: stage === "PENDING_SIGNOFF"
        ? "maria-ap-analyst" : "dan-procurement-manager",
      stage, proposal_id: stage === "PENDING_SIGNOFF" ? null : proposalId,
    } });
  });

  await page.goto("/queue");
  await page.getByRole("textbox", { name: "Email" }).fill("analyst@example.test");
  await page.getByLabel(/Password/).fill("synthetic-analyst-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByRole("button", { name: "SYN-1" }).click();
  await expect(page.getByRole("heading", { name: "Propose a decision" })).toBeVisible();
  await page.getByLabel("Rationale").fill("Synthetic discrepancy needs escalation");
  await page.getByRole("button", { name: "Submit proposal" }).click();
  await expect(page.getByText("Awaiting procurement manager signoff.")).toBeVisible();

  await page.getByRole("button", { name: "Sign out" }).click();
  await page.getByRole("textbox", { name: "Email" }).fill("manager@example.test");
  await page.getByLabel(/Password/).fill("synthetic-manager-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByRole("button", { name: "SYN-1" }).click();
  await expect(page.getByRole("heading", { name: "Independent manager signoff" })).toBeVisible();
  await page.getByLabel("Rationale").fill("Independent synthetic review confirms escalation");
  await page.getByRole("button", { name: "Sign off decision" }).click();
  await expect(page.getByText("The review worker will resume this run.")).toBeVisible();

  expect(decisions).toEqual([
    { token: `Bearer ${analystToken}`, action: "ESCALATE", proposal_id: null },
    { token: `Bearer ${managerToken}`, action: "ESCALATE", proposal_id: proposalId },
  ]);
});

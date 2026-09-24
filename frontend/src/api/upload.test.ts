import { expect, test } from "vitest";
import { uploadInvoice } from "./upload";

class FakeUploadRequest {
  status: number;
  responseText: string;
  timeout = 0;
  method = "";
  url = "";
  body: Document | XMLHttpRequestBodyInit | null = null;
  headers = new Map<string, string>();
  upload = new EventTarget();
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  ontimeout: (() => void) | null = null;
  onabort: (() => void) | null = null;
  completeOnSend = true;

  constructor(status: number, body: unknown) {
    this.status = status;
    this.responseText = JSON.stringify(body);
  }

  open(method: string, url: string) { this.method = method; this.url = url; }
  setRequestHeader(name: string, value: string) { this.headers.set(name, value); }
  send(body: Document | XMLHttpRequestBodyInit | null) {
    this.body = body;
    this.upload.dispatchEvent(new ProgressEvent("progress", {
      lengthComputable: true, loaded: 50, total: 100,
    }));
    if (this.completeOnSend) this.onload?.();
  }
  abort() { this.onabort?.(); }
}

const invoiceId = "00000000-0000-4000-8000-000000000001";
const runId = "00000000-0000-4000-8000-000000000002";
const file = new File(["%PDF-1.7\nsynthetic"], "synthetic.pdf", { type: "application/pdf" });

test("upload reports progress and parses a duplicate response", async () => {
  const request = new FakeUploadRequest(200, {
    invoice_id: invoiceId, run_id: runId, status: "QUEUED", duplicate: true,
  });
  const progress: number[] = [];
  const result = await uploadInvoice(file, "synthetic-service-token", "intake-fixed-key", {
    createRequest: () => request as unknown as XMLHttpRequest,
    onProgress: (percent) => progress.push(percent),
  });
  expect(result.duplicate).toBe(true);
  expect(progress).toEqual([50]);
  expect(request.method).toBe("POST");
  expect(request.url).toContain("/v1/invoices");
  expect(request.headers.get("Authorization")).toBe("Bearer synthetic-service-token");
  expect(request.headers.get("Idempotency-Key")).toBe("intake-fixed-key");
  expect((request.body as FormData).get("file")).toBeInstanceOf(File);
});

test("upload exposes a sanitized problem response for rejection", async () => {
  const request = new FakeUploadRequest(415, {
    type: "about:blank", title: "Unsupported Media Type", status: 415,
    detail: "The uploaded media type is not supported.", instance: "/v1/invoices",
    trace_id: "a".repeat(32),
  });
  await expect(uploadInvoice(file, "synthetic-service-token", "intake-key", {
    createRequest: () => request as unknown as XMLHttpRequest,
  })).rejects.toMatchObject({
    status: 415, message: "The uploaded media type is not supported.",
  });
});

test("cancelling an in-flight upload rejects without a success response", async () => {
  const request = new FakeUploadRequest(201, {
    invoice_id: invoiceId, run_id: runId, status: "QUEUED", duplicate: false,
  });
  request.completeOnSend = false;
  const pending = uploadInvoice(file, "synthetic-service-token", "intake-cancel-key", {
    createRequest: () => request as unknown as XMLHttpRequest,
  });
  request.abort();
  await expect(pending).rejects.toMatchObject({ status: 0, message: "Upload cancelled" });
});

import { ApiError } from "./client";
import { invoiceUploadResponseSchema } from "../schemas/invoice";
import { problemDetailsSchema } from "../schemas/problem";

export interface UploadOptions {
  onProgress?: (percent: number) => void;
  onRequest?: (request: XMLHttpRequest) => void;
  createRequest?: () => XMLHttpRequest;
}

function parseJson(text: string): unknown {
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new ApiError(0, "Upload response was not valid JSON");
  }
}

export function uploadInvoice(
  file: File,
  serviceToken: string,
  idempotencyKey: string,
  options: UploadOptions = {},
) {
  return new Promise<ReturnType<typeof invoiceUploadResponseSchema.parse>>((resolve, reject) => {
    const request = options.createRequest?.() ?? new XMLHttpRequest();
    request.open("POST", `${window.location.origin}/v1/invoices`);
    request.timeout = 45_000;
    request.setRequestHeader("Authorization", `Bearer ${serviceToken}`);
    request.setRequestHeader("Idempotency-Key", idempotencyKey);
    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable && event.total > 0) {
        options.onProgress?.(Math.min(100, Math.round(event.loaded / event.total * 100)));
      }
    });
    request.onload = () => {
      if (request.status === 401 && serviceToken.startsWith("io_")) {
        window.dispatchEvent(new CustomEvent("invoiceops:unauthorized", { detail: serviceToken }));
      }
      try {
        const body = parseJson(request.responseText);
        if (request.status === 200 || request.status === 201) {
          resolve(invoiceUploadResponseSchema.parse(body));
          return;
        }
        const problem = problemDetailsSchema.safeParse(body);
        reject(new ApiError(request.status, problem.success ? problem.data.detail : "Upload rejected"));
      } catch (error) {
        reject(error);
      }
    };
    request.onerror = () => reject(new ApiError(0, "Upload connection failed"));
    request.ontimeout = () => reject(new ApiError(0, "Upload timed out"));
    request.onabort = () => reject(new ApiError(0, "Upload cancelled"));
    const body = new FormData();
    body.append("file", file, file.name);
    options.onRequest?.(request);
    request.send(body);
  });
}

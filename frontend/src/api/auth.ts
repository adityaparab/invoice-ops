import { ApiError } from "./client";
import { problemDetailsSchema } from "../schemas/problem";
import {
  loginRequestSchema, loginResponseSchema, logoutResponseSchema, sessionUserSchema,
} from "../schemas/auth";

async function readResponse(response: Response): Promise<unknown> {
  try { return await response.json() as unknown; }
  catch { throw new ApiError(response.status, "Authentication response was not valid JSON"); }
}

function requireSuccess(response: Response, body: unknown): unknown {
  if (response.ok) return body;
  const problem = problemDetailsSchema.safeParse(body);
  throw new ApiError(response.status, problem.success ? problem.data.detail : "Authentication failed");
}

export async function login(email: string, password: string) {
  const response = await fetch("/v1/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": `login-${crypto.randomUUID()}` },
    body: JSON.stringify(loginRequestSchema.parse({ email, password })),
  });
  return loginResponseSchema.parse(requireSuccess(response, await readResponse(response)));
}

export async function currentUser(token: string) {
  const response = await fetch("/v1/auth/me", {
    headers: { Authorization: `Bearer ${token}` },
  });
  return sessionUserSchema.parse(requireSuccess(response, await readResponse(response)));
}

export async function logout(token: string): Promise<void> {
  const response = await fetch("/v1/auth/logout", {
    method: "POST", headers: {
      Authorization: `Bearer ${token}`,
      "Idempotency-Key": `logout-${crypto.randomUUID()}`,
    },
  });
  logoutResponseSchema.parse(requireSuccess(response, await readResponse(response)));
}

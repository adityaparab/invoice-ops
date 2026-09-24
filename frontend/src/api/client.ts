import createClient from "openapi-fetch";
import { problemDetailsSchema } from "../schemas/problem";
import type { paths } from "../generated/api";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export function apiClient(token?: string) {
  const client = createClient<paths>({ baseUrl: window.location.origin });
  if (token) {
    client.use({
      onRequest({ request }) {
        request.headers.set("Authorization", `Bearer ${token}`);
        return request;
      },
    });
  }
  return client;
}

export function requireData<T>(data: T | undefined, error: unknown, status: number): T {
  if (data !== undefined) return data;
  const problem = problemDetailsSchema.safeParse(error);
  throw new ApiError(status, problem.success ? problem.data.detail : "Request failed");
}

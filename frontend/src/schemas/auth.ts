import { z } from "zod";

export const authRoleSchema = z.enum(["ANALYST", "MANAGER", "AUDITOR", "PLATFORM"]);
export const loginRequestSchema = z.object({
  email: z.email().max(254),
  password: z.string().min(1).max(1024),
});
export const sessionUserSchema = z.object({
  email: z.email(),
  role: authRoleSchema,
});
export const loginResponseSchema = sessionUserSchema.extend({
  token: z.string().startsWith("io_"),
  expires_at: z.iso.datetime({ offset: true }),
});
export const logoutResponseSchema = z.object({ status: z.literal("signed_out") });
export type SessionUser = z.infer<typeof sessionUserSchema>;

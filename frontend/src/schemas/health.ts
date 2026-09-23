import { z } from "zod";

export const dependencyStatusSchema = z.enum([
  "ok",
  "unavailable",
  "timeout",
  "unconfigured",
]);

export const livenessResponseSchema = z.object({ status: z.literal("ok") }).strict();

export const dependencyStatusesSchema = z
  .object({ postgres: dependencyStatusSchema, minio: dependencyStatusSchema })
  .strict();

export const readinessResponseSchema = z
  .object({ status: z.literal("ready"), dependencies: dependencyStatusesSchema })
  .strict();

export type LivenessResponse = z.infer<typeof livenessResponseSchema>;
export type DependencyStatuses = z.infer<typeof dependencyStatusesSchema>;
export type ReadinessResponse = z.infer<typeof readinessResponseSchema>;

import { z } from "zod";

import { dependencyStatusesSchema } from "./health";

export const problemDetailsSchema = z
  .object({
    type: z.string(),
    title: z.string(),
    status: z.number().int().min(400).max(599),
    detail: z.string(),
    instance: z.string(),
    trace_id: z.string().regex(/^[0-9a-f]{32}$/),
    dependencies: dependencyStatusesSchema.nullable().optional(),
  })
  .strict();

export type ProblemDetails = z.infer<typeof problemDetailsSchema>;

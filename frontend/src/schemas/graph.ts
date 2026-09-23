import { z } from "zod";

export const graphStateSchema = z
  .object({
    run_id: z.string().uuid(),
    invoice_id: z.string().uuid(),
    trace_id: z.string().regex(/^[0-9a-f]{32}$/),
    graph_version: z.literal("hello-v1"),
    workflow: z.literal("hello-stubs"),
    status: z.enum(["queued", "running", "completed"]),
    completed_nodes: z.array(z.enum(["hello_start", "hello_finish"])),
  })
  .strict()
  .refine(
    (state) => {
      const expected = {
        queued: [],
        running: ["hello_start"],
        completed: ["hello_start", "hello_finish"],
      };
      return JSON.stringify(state.completed_nodes) === JSON.stringify(expected[state.status]);
    },
    { message: "Completed nodes must match the hello workflow status" },
  );

export type GraphState = z.infer<typeof graphStateSchema>;

import { z } from "zod";

export const emailAttachmentSchema = z
  .object({
    content_type: z.enum(["application/pdf", "image/png", "image/jpeg"]),
    content_base64: z.string().min(1),
  })
  .strict();

export const emailWebhookRequestSchema = z
  .object({ attachment: emailAttachmentSchema })
  .strict();

export type EmailAttachment = z.infer<typeof emailAttachmentSchema>;
export type EmailWebhookRequest = z.infer<typeof emailWebhookRequestSchema>;

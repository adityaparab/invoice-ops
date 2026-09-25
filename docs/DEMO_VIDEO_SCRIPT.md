# InvoiceOps demo video

[Watch the 3-minute-47-second demo](demo/invoiceops-demo.mp4). The browser footage was
recorded against a fresh local Compose stack. The invoice is the versioned synthetic
`SYN-PRICE_MM-0001` golden sample. The worker used the operator's LiteLLM URL and API key
with model-name aliases from `.env.example`; the Evals screen reads committed reports.

| Edited time | Screen and action | Narration |
| --- | --- | --- |
| 0:00–0:16 | Open Maria's workspace and Intake. | InvoiceOps takes a synthetic invoice from intake to an auditable decision. Maria starts with a document upload; every step that follows is tied to this run. |
| 0:16–1:00 | Upload the price-mismatch PNG, show the assigned IDs, then follow Agent Run while the real worker processes it. | The service accepts the image, deduplicates its content, and queues a durable run. Extraction is model-assisted through the configured LiteLLM gateway. Validation, purchase-order matching, and policy checks use deterministic rules. The run monitor displays committed node events rather than guessing at unseen progress. |
| 1:00–1:33 | Show the run paused at HumanReview, then open the exception queue. | The invoice carries a price mismatch, so the graph stops for a person. It does not grant automatic approval. The queue shows the exception type, priority, service deadline, and current review status. |
| 1:33–2:14 | Open the invoice's three-way comparison and extracted fields. | Maria can inspect the invoice beside the purchase order and goods receipt. The line's unit price is fifty-eight euros and thirty cents on the invoice, against fifty-three euros and thirty cents on the purchase order. The mismatch is visible in the evidence, along with field-level extraction confidence and masked bank details. She records an escalation proposal with a rationale. |
| 2:14–2:51 | Show Maria's proposal, switch to Dan, and load the pending signoff. | A proposal alone cannot finish this decision. Dan uses a separate procurement-manager identity to review the same evidence and Maria's recorded rationale. The two-person rule is enforced by the API, not only by the screen. |
| 2:51–3:10 | Dan signs off; the UI confirms the review worker is queued. | Dan independently confirms the price discrepancy. The decision is queued for workflow resume; the demo does not claim that the downstream resume has already run. |
| 3:10–3:35 | Switch to Priya, open the run's audit history, and show the event timeline. | Priya sees the immutable sequence of ingestion, extraction, matching, policy, triage, and both human actions. Each event identifies its actor and pins the versions needed to reconstruct the decision. |
| 3:35–3:47 | Open Evals on the ADK/Gemini comparison report. | The separate ADK comparison processed the full five-hundred-invoice golden set three times. The screen reports measured quality and its limits: the proxy reported zero Gemini cost, so actual provider spend is still unverified. |

The live workflow in the video uses the LangGraph production default. The closing ADK report
is an independent comparison run with a Gemini model route. Differences between those reports
cannot isolate a framework effect. The video uses synthetic vendors and local demo credentials;
no customer invoice or production key appears on screen.

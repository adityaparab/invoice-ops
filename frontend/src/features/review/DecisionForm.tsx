import { zodResolver } from "@hookform/resolvers/zod";
import { Alert, Button, Select, Text, Textarea, TextInput, Title } from "@mantine/core";
import { Controller, useForm } from "react-hook-form";
import { decisionRequestSchema } from "../../schemas/decision";
import type { DecisionRequest } from "../../schemas/decision";
import type { InvoiceDetail } from "../../schemas/invoice_read";
import type { PersonaId } from "../../app/persona";
import { useDecision } from "./useReview";
import styles from "./ExceptionReview.module.css";

interface Props {
  detail: InvoiceDetail;
  token: string;
  persona: PersonaId;
}

export function DecisionForm({ detail, token, persona }: Props) {
  const exception = detail.exception;
  const mutation = useDecision(token, exception?.id ?? "", detail.invoice.id);
  const form = useForm<DecisionRequest>({
    resolver: zodResolver(decisionRequestSchema),
    defaultValues: {
      action: detail.pending_proposal?.action ?? "ESCALATE",
      rationale: "",
      reason_code: detail.pending_proposal?.reason_code ?? "MANUAL_REVIEW",
      proposal_id: persona === "dan" ? detail.pending_proposal?.id ?? null : null,
    },
  });

  if (!exception || detail.invoice.run_status !== "PAUSED") return null;
  if (exception.status !== "OPEN" && exception.status !== "IN_REVIEW") return null;
  if (persona === "maria" && exception.status !== "OPEN") {
    return <Text>Awaiting procurement manager signoff.</Text>;
  }
  if (persona === "dan" && (exception.status !== "IN_REVIEW" || !detail.pending_proposal)) {
    return <Text>Awaiting an analyst proposal.</Text>;
  }
  if (persona !== "maria" && persona !== "dan") return null;

  const proposal = detail.pending_proposal;
  const actions = [
    { value: "APPROVE", label: "Approve", disabled: persona === "dan" && proposal?.action !== "APPROVE" },
    { value: "RETURN", label: "Return to vendor" },
    { value: "ESCALATE", label: "Escalate" },
  ];

  return (
    <section className={styles.card} aria-labelledby="decision-title">
      <Title order={3} id="decision-title">
        {persona === "maria" ? "Propose a decision" : "Independent manager signoff"}
      </Title>
      {proposal && persona === "dan" && (
        <div className={styles.proposal}>
          <Text>Analyst proposed {proposal.action.toLowerCase().replaceAll("_", " ")} · {proposal.reason_code}</Text>
          <Text>{proposal.rationale}</Text>
          <Text>Proposed by {proposal.actor_id}</Text>
        </div>
      )}
      <form className={styles.decisionForm} onSubmit={form.handleSubmit((input) => mutation.mutate(input))}>
        <Controller
          name="action"
          control={form.control}
          render={({ field }) => (
            <Select
              label="Decision"
              data={actions}
              value={field.value}
              onChange={(value) => field.onChange(value)}
              error={form.formState.errors.action?.message}
              required
            />
          )}
        />
        <Controller
          name="reason_code"
          control={form.control}
          render={({ field }) => (
            <TextInput
              label="Reason code"
              description="Uppercase letters, numbers, and underscores"
              value={field.value}
              onChange={field.onChange}
              error={form.formState.errors.reason_code?.message}
              required
            />
          )}
        />
        <Controller
          name="rationale"
          control={form.control}
          render={({ field }) => (
            <Textarea
              label="Rationale"
              value={field.value}
              onChange={field.onChange}
              error={form.formState.errors.rationale?.message}
              minRows={3}
              required
            />
          )}
        />
        {mutation.isError && (
          <Alert title="Decision could not be saved">
            {mutation.error instanceof Error ? mutation.error.message : "Please retry."}
          </Alert>
        )}
        {mutation.isSuccess && (
          <Alert title={mutation.data.stage === "PENDING_SIGNOFF" ? "Proposal saved" : "Decision queued"}>
            {mutation.data.stage === "PENDING_SIGNOFF"
              ? "A different manager must sign off."
              : "The review worker will resume this run."}
          </Alert>
        )}
        <Button type="submit" disabled={mutation.isPending || mutation.isSuccess}>
          {mutation.isPending ? "Saving…" : persona === "maria" ? "Submit proposal" : "Sign off decision"}
        </Button>
      </form>
    </section>
  );
}

import { Alert, Badge, Button, Table, Text, TextInput, Timeline, Title } from "@mantine/core";
import { useMutation } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router";
import { z } from "zod";
import { usePersona } from "../../app/persona";
import type { LedgerEvent } from "../../schemas/ledger";
import { collectAuditRun, downloadAuditRun } from "./export";
import { useAuditRun } from "./useAudit";
import styles from "./Audit.module.css";

const runIdSchema = z.string().uuid();

function VersionPins({ event }: { event: LedgerEvent }) {
  return (
    <dl className={styles.pins}>
      <div><dt>Graph</dt><dd>{event.versions.graph_version}</dd></div>
      <div><dt>Model</dt><dd>{event.versions.model_version}</dd></div>
      <div><dt>Prompt</dt><dd>{event.versions.prompt_version}</dd></div>
      <div><dt>Policy</dt><dd>{event.versions.policy_version}</dd></div>
    </dl>
  );
}

export function Audit() {
  const { token } = usePersona();
  const [searchParams, setSearchParams] = useSearchParams();
  const queryRunId = searchParams.get("run_id");
  const runId = queryRunId && runIdSchema.safeParse(queryRunId).success ? queryRunId : null;
  const [draftId, setDraftId] = useState(queryRunId ?? "");
  const audit = useAuditRun(token, runId);
  const first = audit.data?.pages[0];
  const events = audit.data?.pages.flatMap((page) => page.events) ?? [];
  const exportMutation = useMutation({
    mutationFn: async () => {
      if (runId === null) throw new Error("Choose a run first.");
      const document = await collectAuditRun(token, runId);
      downloadAuditRun(runId, document);
      return document.events.length;
    },
  });

  useEffect(() => { setDraftId(queryRunId ?? ""); }, [queryRunId]);

  return (
    <div className={styles.screen}>
      <div>
        <Text className={styles.eyebrow}>Risk and controls</Text>
        <Title order={2}>Audit & Provenance</Title>
        <Text className={styles.muted}>Reconstruct a run from immutable events, actors, and version pins.</Text>
      </div>
      <section className={styles.card} aria-labelledby="audit-lookup-title">
        <Title order={3} id="audit-lookup-title">Find a run</Title>
        <form className={styles.lookup} onSubmit={(event) => {
          event.preventDefault();
          if (runIdSchema.safeParse(draftId.trim()).success) {
            setSearchParams({ run_id: draftId.trim() });
          }
        }}>
          <TextInput
            label="Run ID"
            placeholder="Paste a run UUID"
            value={draftId}
            onChange={(event) => setDraftId(event.currentTarget.value)}
            error={draftId.length > 0 && !runIdSchema.safeParse(draftId.trim()).success
              ? "Enter a valid run UUID" : undefined}
          />
          <Button type="submit" disabled={!runIdSchema.safeParse(draftId.trim()).success}>Open history</Button>
        </form>
      </section>
      {token.length === 0 && <Alert title="Auditor token required">Enter Priya’s auditor token in the sidebar.</Alert>}
      {runId && token && audit.isPending && <Text>Loading audit history…</Text>}
      {runId && token && audit.isError && <Alert title="Audit history unavailable">{audit.error.message}</Alert>}
      {first && (
        <>
          <section className={styles.card} aria-labelledby="audit-summary-title">
            <div className={styles.heading}>
              <Title order={3} id="audit-summary-title">Run provenance</Title>
              <Badge>{first.status}</Badge>
            </div>
            <dl className={styles.metadata}>
              <div><dt>Run ID</dt><dd>{first.run_id}</dd></div>
              <div><dt>Invoice ID</dt><dd>{first.invoice_id}</dd></div>
              <div><dt>Trace ID</dt><dd>{first.trace_id}</dd></div>
              <div><dt>Events loaded</dt><dd>{events.length}</dd></div>
            </dl>
            <div className={styles.actions}>
              <Button onClick={() => exportMutation.mutate()} loading={exportMutation.isPending}>
                Export full run provenance
              </Button>
              <Button className={styles.secondaryButton} onClick={() => audit.refetch()}>Refresh</Button>
            </div>
            {exportMutation.isError && <Alert title="Export failed">{exportMutation.error.message}</Alert>}
            {exportMutation.isSuccess && <Text>{exportMutation.data} events exported.</Text>}
          </section>
          <section className={styles.card} aria-labelledby="audit-timeline-title">
            <Title order={3} id="audit-timeline-title">Run trace</Title>
            {events.length === 0 ? <Text>No events recorded for this run.</Text> : (
              <Timeline className={styles.timeline}>
                {events.map((event) => (
                  <Timeline.Item key={event.id} title={`${event.sequence}. ${event.event_type}`}>
                    <Text className={styles.muted}>
                      {new Date(event.created_at).toLocaleString()} · {event.node ?? "System"} · {event.actor_type}
                    </Text>
                  </Timeline.Item>
                ))}
              </Timeline>
            )}
          </section>
          <section className={styles.card} aria-labelledby="ledger-title">
            <Title order={3} id="ledger-title">Full ledger</Title>
            <div className={styles.tableWrap}>
              <Table>
                <Table.Thead><Table.Tr>
                  <Table.Th>Seq.</Table.Th><Table.Th>Event and node</Table.Th>
                  <Table.Th>Actor</Table.Th><Table.Th>Versions and evidence</Table.Th>
                </Table.Tr></Table.Thead>
                <Table.Tbody>
                  {events.map((event) => (
                    <Table.Tr key={event.id}>
                      <Table.Td>{event.sequence}</Table.Td>
                      <Table.Td><strong>{event.event_type}</strong><Text>{event.node ?? "System"}</Text></Table.Td>
                      <Table.Td><strong>{event.actor_type}</strong><Text>{event.actor_id}</Text></Table.Td>
                      <Table.Td>
                        <VersionPins event={event} />
                        <details>
                          <summary>Payload and correction</summary>
                          <Text>Event ID: {event.id}</Text>
                          {event.supersedes_id && <Text>Supersedes: {event.supersedes_id}</Text>}
                          <pre className={styles.payload}>{JSON.stringify(event.payload, null, 2)}</pre>
                        </details>
                      </Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </div>
            {audit.hasNextPage && (
              <Button className={styles.secondaryButton} onClick={() => audit.fetchNextPage()} loading={audit.isFetchingNextPage}>
                Load more events
              </Button>
            )}
          </section>
        </>
      )}
    </div>
  );
}

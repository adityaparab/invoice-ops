import { Alert, Badge, Button, Text, TextInput, Title, UnstyledButton } from "@mantine/core";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { z } from "zod";
import { usePersona } from "../../app/persona";
import type { NodeProgress } from "../../schemas/run_progress";
import { useRunProgress } from "./useRunProgress";
import styles from "./AgentRun.module.css";

const runIdSchema = z.string().uuid();

function statusLabel(node: NodeProgress, activeNode: string | null): string {
  if (node.name === activeNode) return "Current";
  return node.observed_at ? "Recorded" : "Waiting";
}

export function AgentRun() {
  const { token } = usePersona();
  const [searchParams, setSearchParams] = useSearchParams();
  const queryRunId = searchParams.get("run_id");
  const runId = queryRunId && runIdSchema.safeParse(queryRunId).success ? queryRunId : null;
  const [draftId, setDraftId] = useState(queryRunId ?? "");
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const progress = useRunProgress(token, runId);
  const run = progress.data;
  const selected = run?.nodes.find((node) => node.name === selectedNode) ?? null;

  useEffect(() => { setDraftId(queryRunId ?? ""); }, [queryRunId]);
  useEffect(() => { setSelectedNode(null); }, [runId]);

  return (
    <div className={styles.screen}>
      <div>
        <Text className={styles.eyebrow}>Workflow monitor</Text>
        <Title order={2}>Agent Run</Title>
        <Text className={styles.muted}>Follow recorded node outputs and inspect the bounded state each node produced.</Text>
      </div>
      <section className={styles.card} aria-labelledby="run-lookup-title">
        <Title order={3} id="run-lookup-title">Find a run</Title>
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
          <Button type="submit" disabled={!runIdSchema.safeParse(draftId.trim()).success}>Inspect run</Button>
        </form>
        <Text className={styles.muted}>Open a run from Intake or Exception Review, or paste its ID here.</Text>
      </section>
      {token.length === 0 && <Alert title="Token required">Enter this persona’s API token in the sidebar.</Alert>}
      {runId && token && progress.isPending && <Text>Loading run progress…</Text>}
      {runId && token && progress.isError && <Alert title="Run unavailable">{progress.error.message}</Alert>}
      {run && (
        <>
          <section className={styles.card} aria-labelledby="run-status-title">
            <div className={styles.heading}>
              <Title order={3} id="run-status-title">Run status</Title>
              <Badge>{run.status}</Badge>
            </div>
            <dl className={styles.metadata}>
              <div><dt>Run ID</dt><dd>{run.run_id}</dd></div>
              <div><dt>Invoice ID</dt><dd>{run.invoice_id}</dd></div>
              <div><dt>Graph version</dt><dd>{run.graph_version}</dd></div>
              <div><dt>Current node</dt><dd>{run.active_node ?? "—"}</dd></div>
              <div><dt>Last read</dt><dd>{new Date(run.read_at).toLocaleString()}</dd></div>
            </dl>
            <Text className={styles.muted}>Node markers reflect committed audit events. The current node is inferred from those events and the run status.</Text>
            <Link className={styles.link} to="/queue">Open Exception Review</Link>
          </section>
          <section className={styles.card} aria-labelledby="node-progress-title">
            <Title order={3} id="node-progress-title">Node progress</Title>
            <div className={styles.nodes}>
              {run.nodes.map((node) => (
                <UnstyledButton
                  key={node.name}
                  className={`${styles.node} ${selectedNode === node.name ? styles.selected : ""}`}
                  onClick={() => setSelectedNode(node.name)}
                  aria-pressed={selectedNode === node.name}
                >
                  <strong>{node.name}</strong>
                  <span>{statusLabel(node, run.active_node)}</span>
                </UnstyledButton>
              ))}
            </div>
          </section>
          {selected && (
            <section className={styles.card} aria-labelledby="node-state-title">
              <div className={styles.heading}>
                <Title order={3} id="node-state-title">{selected.name} state</Title>
                <Badge>{statusLabel(selected, run.active_node)}</Badge>
              </div>
              {selected.observed_at ? (
                <>
                  <Text className={styles.muted}>{selected.event_type} · {new Date(selected.observed_at).toLocaleString()}</Text>
                  <pre className={styles.state}>{JSON.stringify(selected.state, null, 2)}</pre>
                </>
              ) : <Text>No audited output has been recorded for this node.</Text>}
            </section>
          )}
        </>
      )}
    </div>
  );
}

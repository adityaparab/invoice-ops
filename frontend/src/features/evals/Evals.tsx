import { Alert, Badge, Select, Table, Text, Title } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { CartesianGrid, Line, LineChart, Tooltip, XAxis, YAxis } from "recharts";
import { fetchEvalDashboard } from "../../api/evals";
import { usePersona } from "../../app/persona";
import type { EvalDashboard, EvalReport, MetricRow } from "../../schemas/evals";
import styles from "./Evals.module.css";

function metricValue(metric: MetricRow): string {
  if (metric.unit === "rate") return `${(Number(metric.value) * 100).toFixed(2)}%`;
  if (metric.unit === "usd") return `$${metric.value}`;
  if (metric.unit === "ms") return `${metric.value} ms`;
  return metric.value;
}

function ReportContent({ report, data }: { report: EvalReport; data: EvalDashboard }) {
  const chartData = report.tau_sweep.map((point) => ({
    threshold: Number(point.threshold),
    exception_recall: Number(point.exception_recall),
    false_escalation_rate: Number(point.false_escalation_rate),
    stp_rate: Number(point.stp_rate),
  }));
  return (
    <>
      <section className={styles.card} aria-labelledby="eval-report-title">
        <div className={styles.heading}>
          <Title order={3} id="eval-report-title">{report.title}</Title>
          <Badge>{report.report_version}</Badge>
        </div>
        <dl className={styles.metadata}>
          <div><dt>Dataset</dt><dd>{report.dataset_version}</dd></div>
          <div><dt>Measured</dt><dd>{new Date(report.measured_at).toLocaleString()}</dd></div>
          <div><dt>Model versions</dt><dd>{report.model_versions.join(", ") || "None recorded"}</dd></div>
        </dl>
        {report.caveats.map((caveat) => <Text className={styles.caveat} key={caveat}>{caveat}</Text>)}
      </section>
      <section className={styles.card} aria-labelledby="eval-metrics-title">
        <Title order={3} id="eval-metrics-title">Versioned metrics</Title>
        {report.metrics.length === 0 ? <Text>No metrics have been recorded for this report.</Text> : (
          <div className={styles.tableWrap}>
            <Table>
              <Table.Thead><Table.Tr>
                <Table.Th>Scope</Table.Th><Table.Th>Metric</Table.Th><Table.Th>Value</Table.Th>
                <Table.Th>Count</Table.Th><Table.Th>TP</Table.Th><Table.Th>FP</Table.Th><Table.Th>FN</Table.Th>
              </Table.Tr></Table.Thead>
              <Table.Tbody>{report.metrics.map((metric) => (
                <Table.Tr key={`${metric.scope}:${metric.key}`}>
                  <Table.Td>{metric.scope}</Table.Td><Table.Td>{metric.label}</Table.Td>
                  <Table.Td>{metricValue(metric)}</Table.Td>
                  <Table.Td>{metric.sample_count ?? "—"}</Table.Td>
                  <Table.Td>{metric.tp ?? "—"}</Table.Td>
                  <Table.Td>{metric.fp ?? "—"}</Table.Td>
                  <Table.Td>{metric.fn ?? "—"}</Table.Td>
                </Table.Tr>
              ))}</Table.Tbody>
            </Table>
          </div>
        )}
      </section>
      <div className={styles.analysisGrid}>
        <section className={styles.card} aria-labelledby="eval-confusion-title">
          <Title order={3} id="eval-confusion-title">Per-anomaly confusion</Title>
          {report.per_anomaly_confusion.length === 0 ? <Text>No per-anomaly confusion has been measured for this report.</Text> : (
            <div className={styles.tableWrap}>
              <Table>
                <Table.Thead><Table.Tr>
                  <Table.Th>Anomaly</Table.Th><Table.Th>TP</Table.Th><Table.Th>FP</Table.Th>
                  <Table.Th>FN</Table.Th><Table.Th>TN</Table.Th>
                </Table.Tr></Table.Thead>
                <Table.Tbody>{report.per_anomaly_confusion.map((row) => (
                  <Table.Tr key={row.anomaly_code}>
                    <Table.Td>{row.anomaly_code}</Table.Td><Table.Td>{row.tp}</Table.Td>
                    <Table.Td>{row.fp}</Table.Td><Table.Td>{row.fn}</Table.Td><Table.Td>{row.tn}</Table.Td>
                  </Table.Tr>
                ))}</Table.Tbody>
              </Table>
            </div>
          )}
        </section>
        <section className={styles.card} aria-labelledby="eval-tau-title">
          <Title order={3} id="eval-tau-title">τ sweep</Title>
          {chartData.length === 0 ? <Text>No threshold sweep has been measured for this report.</Text> : (
            <>
              <Text className={styles.muted}>Threshold versus exception recall, false escalation, and straight-through processing</Text>
              <LineChart className={styles.chart} responsive data={chartData}>
                <CartesianGrid vertical={false} />
                <XAxis dataKey="threshold" />
                <YAxis domain={[0, 1]} />
                <Tooltip />
                <Line className={styles.recallLine} dataKey="exception_recall" name="Exception recall" />
                <Line className={styles.falseLine} dataKey="false_escalation_rate" name="False escalation" />
                <Line className={styles.stpLine} dataKey="stp_rate" name="STP rate" />
              </LineChart>
            </>
          )}
        </section>
      </div>
      <section className={styles.card} aria-labelledby="experiment-log-title">
        <Title order={3} id="experiment-log-title">Experiment log</Title>
        {data.experiments.length === 0 ? <Text>No experiments have been recorded.</Text> : (
          <div className={styles.experiments}>
            {data.experiments.map((entry) => (
              <article className={styles.experiment} key={entry.id}>
                <div className={styles.heading}>
                  <Title order={4}>{entry.date}</Title>
                  <Badge>{entry.report_id}</Badge>
                </div>
                <dl>
                  <div><dt>Hypothesis</dt><dd>{entry.hypothesis}</dd></div>
                  <div><dt>Change</dt><dd>{entry.change}</dd></div>
                  <div><dt>Observation</dt><dd>{entry.observation}</dd></div>
                  <div><dt>Decision</dt><dd>{entry.decision}</dd></div>
                </dl>
              </article>
            ))}
          </div>
        )}
      </section>
    </>
  );
}

export function Evals() {
  const { token } = usePersona();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const dashboard = useQuery({
    queryKey: ["eval-reports"],
    queryFn: () => fetchEvalDashboard(token),
    enabled: token.length > 0,
  });
  const reports = dashboard.data?.reports ?? [];
  const report = reports.find((item) => item.report_id === selectedId) ?? reports[0];
  useEffect(() => {
    if (reports.length > 0 && !reports.some((item) => item.report_id === selectedId)) {
      setSelectedId(reports[0]?.report_id ?? null);
    }
  }, [reports, selectedId]);
  return (
    <div className={styles.screen}>
      <div className={styles.header}>
        <div>
          <Text className={styles.eyebrow}>Measured quality</Text>
          <Title order={2}>Evals</Title>
          <Text className={styles.muted}>Versioned reports and experiment decisions from committed evaluation artifacts.</Text>
        </div>
        {reports.length > 0 && (
          <Select
            className={styles.reportSelect}
            label="Report"
            data={reports.map((item) => ({ value: item.report_id, label: item.title }))}
            value={report?.report_id ?? null}
            onChange={setSelectedId}
          />
        )}
      </div>
      {token.length > 0 && dashboard.isPending && <Text>Loading evaluation reports…</Text>}
      {dashboard.isError && <Alert title="Evals unavailable">{dashboard.error.message}</Alert>}
      {dashboard.data && reports.length === 0 && <Text>No evaluation reports are available.</Text>}
      {dashboard.data && report && <ReportContent report={report} data={dashboard.data} />}
    </div>
  );
}

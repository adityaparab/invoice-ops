import { Alert, Button, Select, Text, Title } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { Bar, BarChart, CartesianGrid, Tooltip, XAxis, YAxis } from "recharts";
import { useState } from "react";
import { Link } from "react-router";
import { fetchDashboard } from "../../api/dashboard";
import { usePersona } from "../../app/persona";
import type { DashboardSummary } from "../../schemas/dashboard";
import styles from "./Dashboard.module.css";

function rateLabel(value: string | null): string {
  return value === null ? "—" : `${(Number(value) * 100).toFixed(1)}%`;
}

function CostLabel({ data }: { data: DashboardSummary }) {
  const cost = data.cost_per_observed_invoice_usd;
  return (
    <div className={styles.metricCard}>
      <Text className={styles.metricLabel}>Cost per observed invoice</Text>
      <Text className={styles.metricValue}>{cost === null ? "—" : `$${cost}`}</Text>
      <Text className={styles.metricNote}>
        {data.cost_observed_invoices} of {data.invoice_count} invoices have reported model cost
      </Text>
      <Text className={styles.coverage}>
        {data.cost_coverage === "UNAVAILABLE" ? "No cost data"
          : data.cost_coverage === "PARTIAL" ? "Partial cost data" : "Complete cost data"}
      </Text>
    </div>
  );
}

function DashboardContent({ data }: { data: DashboardSummary }) {
  const aging = [
    { label: "Under 24h", count: data.aging.under_24_hours },
    { label: "1–3 days", count: data.aging.one_to_three_days },
    { label: "Over 3 days", count: data.aging.over_three_days },
  ];
  const volumes = data.volume_by_day.map((point) => ({ day: point.day.slice(5), invoices: point.invoices }));
  const exceptions = data.exception_types.slice(0, 10);
  return (
    <>
      <div className={styles.metrics}>
        <div className={styles.metricCard}>
          <Text className={styles.metricLabel}>Invoices received</Text>
          <Text className={styles.metricValue}>{data.invoice_count}</Text>
          <Text className={styles.metricNote}>{data.resolved_count} resolved in period</Text>
        </div>
        <div className={styles.metricCard}>
          <Text className={styles.metricLabel}>Straight-through processing</Text>
          <Text className={styles.metricValue}>{rateLabel(data.stp_rate)}</Text>
          <Text className={styles.metricNote}>{data.auto_approved_count} auto-approved of {data.resolved_count} resolved</Text>
        </div>
        <div className={styles.metricCard}>
          <Text className={styles.metricLabel}>Open exceptions</Text>
          <Text className={styles.metricValue}>{data.open_exception_count}</Text>
          <Text className={styles.metricNote}>{data.aging.sla_overdue} past SLA</Text>
          <Link className={styles.drillLink} to="/queue">Open review queue</Link>
        </div>
        <CostLabel data={data} />
      </div>
      <div className={styles.chartGrid}>
        <section className={styles.chartCard} aria-labelledby="volume-chart-title">
          <Title order={3} id="volume-chart-title">Daily invoice volume</Title>
          <Text className={styles.chartNote}>UTC calendar days</Text>
          <BarChart className={styles.chart} responsive data={volumes}>
            <CartesianGrid vertical={false} />
            <XAxis dataKey="day" />
            <YAxis allowDecimals={false} />
            <Tooltip />
            <Bar dataKey="invoices" />
          </BarChart>
        </section>
        <section className={styles.chartCard} aria-labelledby="aging-chart-title">
          <Title order={3} id="aging-chart-title">Open exception aging</Title>
          <Text className={styles.chartNote}>Age since the latest exception was created</Text>
          <BarChart className={styles.chart} responsive data={aging}>
            <CartesianGrid vertical={false} />
            <XAxis dataKey="label" />
            <YAxis allowDecimals={false} />
            <Tooltip />
            <Bar dataKey="count" />
          </BarChart>
        </section>
        <section className={styles.chartCard} aria-labelledby="exceptions-chart-title">
          <div className={styles.sectionHeader}>
            <Title order={3} id="exceptions-chart-title">Exception types</Title>
            <Link className={styles.drillLink} to="/queue">Review exceptions</Link>
          </div>
          {exceptions.length === 0 ? <Text>No exceptions in this period.</Text> : (
            <BarChart className={styles.chart} responsive data={exceptions}>
              <CartesianGrid vertical={false} />
              <XAxis dataKey="code" />
              <YAxis allowDecimals={false} />
              <Tooltip />
              <Bar dataKey="count" />
            </BarChart>
          )}
        </section>
      </div>
    </>
  );
}

export function Dashboard() {
  const { token } = usePersona();
  const [periodDays, setPeriodDays] = useState(30);
  const dashboard = useQuery({
    queryKey: ["dashboard", periodDays],
    queryFn: () => fetchDashboard(token, periodDays),
    enabled: token.length > 0,
  });
  return (
    <div className={styles.screen}>
      <div className={styles.header}>
        <div>
          <Text className={styles.eyebrow}>Procurement operations</Text>
          <Title order={2}>Dashboard</Title>
          <Text className={styles.muted}>Audited invoice volume, review load, and observed model cost.</Text>
        </div>
        <Select
          className={styles.period}
          label="Reporting period"
          data={[7, 30, 90].map((days) => ({ value: String(days), label: `Last ${days} days` }))}
          value={String(periodDays)}
          onChange={(value) => setPeriodDays(Number(value ?? 30))}
        />
      </div>
      {token.length === 0 && <Alert title="Manager token required">Enter Dan’s API token in the sidebar.</Alert>}
      {dashboard.isPending && token.length > 0 && <Text>Loading dashboard…</Text>}
      {dashboard.isError && <Alert title="Dashboard unavailable">{dashboard.error.message}</Alert>}
      {dashboard.data && <DashboardContent data={dashboard.data} />}
      {dashboard.data && <Button component={Link} to="/queue">Go to Exception Review</Button>}
    </div>
  );
}

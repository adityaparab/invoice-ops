import { Badge, Button, Text, Title } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { lazy, Suspense } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router";
import { fetchHealth } from "../api/health";
import { personaLabels, usePersona } from "./persona";
import { Login } from "./Login";
import { appRoutes, routesFor } from "./routes";
import type { AppRoute } from "./routes";
import styles from "./App.module.css";

const ExceptionReview = lazy(() => import("../features/review/ExceptionReview").then(
  (module) => ({ default: module.ExceptionReview }),
));
const Dashboard = lazy(() => import("../features/dashboard/Dashboard").then(
  (module) => ({ default: module.Dashboard }),
));
const Intake = lazy(() => import("../features/intake/Intake").then(
  (module) => ({ default: module.Intake }),
));
const AgentRun = lazy(() => import("../features/runs/AgentRun").then(
  (module) => ({ default: module.AgentRun }),
));
const Audit = lazy(() => import("../features/audit/Audit").then(
  (module) => ({ default: module.Audit }),
));
const Evals = lazy(() => import("../features/evals/Evals").then(
  (module) => ({ default: module.Evals }),
));

function HealthStatus() {
  const health = useQuery({
    queryKey: ["health"],
    queryFn: fetchHealth,
    refetchInterval: 30_000,
  });
  return (
    <Badge className={styles.health}>
      {health.isPending ? "Checking API" : health.isError ? "API unavailable" : "API online"}
    </Badge>
  );
}

function ScreenPlaceholder({ route }: { route: AppRoute }) {
  return (
    <section className={styles.placeholder} aria-labelledby="screen-title">
      <Text className={styles.eyebrow}>InvoiceOps workspace</Text>
      <Title order={2} id="screen-title">{route.label}</Title>
      <Text>{route.description}</Text>
      <Text className={styles.placeholderNote}>
        This screen is being connected to the API in the next implementation steps.
      </Text>
      <Button component="a" href="/healthz" target="_blank" rel="noreferrer">
        Check API health
      </Button>
    </section>
  );
}

function GuardedScreen({ route }: { route: AppRoute }) {
  const { persona } = usePersona();
  if (!route.personas.includes(persona)) {
    const home = routesFor(persona)[0];
    return <Navigate to={home?.path ?? "/queue"} replace />;
  }
  if (route.path === "/queue") return <Suspense fallback={<Text>Loading review…</Text>}><ExceptionReview /></Suspense>;
  if (route.path === "/dashboard") return <Suspense fallback={<Text>Loading dashboard…</Text>}><Dashboard /></Suspense>;
  if (route.path === "/intake") return <Suspense fallback={<Text>Loading intake…</Text>}><Intake /></Suspense>;
  if (route.path === "/runs") return <Suspense fallback={<Text>Loading run…</Text>}><AgentRun /></Suspense>;
  if (route.path === "/audit") return <Suspense fallback={<Text>Loading audit…</Text>}><Audit /></Suspense>;
  if (route.path === "/evals") return <Suspense fallback={<Text>Loading evals…</Text>}><Evals /></Suspense>;
  return <ScreenPlaceholder route={route} />;
}

export function App() {
  const { persona, email, authenticated, loading, signOut } = usePersona();
  if (loading) return <Text className={styles.loading}>Checking session…</Text>;
  if (!authenticated) return <Login />;
  const available = routesFor(persona);
  const home = available[0];
  return (
    <div className={styles.shell}>
      <aside className={styles.sidebar}>
        <div className={styles.brand}>
          <Text className={styles.brandMark}>IO</Text>
          <div>
            <Title order={1} className={styles.brandTitle}>InvoiceOps</Title>
            <Text className={styles.brandSubtitle}>Operations console</Text>
          </div>
        </div>
        <Text className={styles.signedInEmail}>{email}</Text>
        <nav className={styles.nav} aria-label="Workspace">
          {available.map((route) => (
            <NavLink
              key={route.path}
              to={route.path}
              className={({ isActive }) =>
                isActive ? `${styles.navItem} ${styles.navItemActive}` : styles.navItem
              }
            >
              {route.label}
            </NavLink>
          ))}
        </nav>
        <Button variant="subtle" className={styles.signOut} onClick={() => { void signOut(); }}>Sign out</Button>
      </aside>
      <main className={styles.main}>
        <header className={styles.topbar}>
          <div>
            <Text className={styles.topbarLabel}>Signed in as</Text>
            <Text className={styles.topbarPersona}>{personaLabels[persona]}</Text>
          </div>
          <HealthStatus />
        </header>
        <div className={styles.content}>
          <Routes>
            <Route path="/" element={<Navigate to={home?.path ?? "/queue"} replace />} />
            {appRoutes.map((route) => (
              <Route key={route.path} path={route.path} element={<GuardedScreen route={route} />} />
            ))}
            <Route path="*" element={<Navigate to={home?.path ?? "/queue"} replace />} />
          </Routes>
        </div>
      </main>
    </div>
  );
}

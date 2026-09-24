import { Badge, Button, PasswordInput, Select, Text, Title } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { NavLink, Navigate, Route, Routes } from "react-router";
import { fetchHealth } from "../api/health";
import { isPersona, personaIds, personaLabels, usePersona } from "./persona";
import { appRoutes, routesFor } from "./routes";
import type { AppRoute } from "./routes";
import styles from "./App.module.css";

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

function PersonaSwitcher() {
  const { persona, token, choosePersona, setToken } = usePersona();
  return (
    <form className={styles.personaControls} onSubmit={(event) => event.preventDefault()}>
      <Select
        label="Workspace persona"
        data={personaIds.map((id) => ({ value: id, label: personaLabels[id] }))}
        value={persona}
        onChange={(value) => {
          if (value && isPersona(value)) choosePersona(value);
        }}
      />
      {persona !== "platform" && (
        <PasswordInput
          label="Persona API token"
          description="Entered token stays in this tab only"
          value={token}
          onChange={(event) => setToken(event.currentTarget.value)}
        />
      )}
    </form>
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
  return <ScreenPlaceholder route={route} />;
}

export function App() {
  const { persona } = usePersona();
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
        <PersonaSwitcher />
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
        <Text className={styles.sidebarFootnote}>Human decisions remain auditable.</Text>
      </aside>
      <main className={styles.main}>
        <header className={styles.topbar}>
          <div>
            <Text className={styles.topbarLabel}>Current persona</Text>
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

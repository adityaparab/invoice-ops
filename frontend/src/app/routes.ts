import type { PersonaId } from "./persona";

export interface AppRoute {
  path: string;
  label: string;
  description: string;
  personas: readonly PersonaId[];
}

export const appRoutes: readonly AppRoute[] = [
  {
    path: "/dashboard",
    label: "Dashboard",
    description: "Volumes, straight-through processing, aging, and cost",
    personas: ["dan"],
  },
  {
    path: "/queue",
    label: "Exception Review",
    description: "Prioritized invoice queue and evidence review",
    personas: ["maria", "dan"],
  },
  {
    path: "/intake",
    label: "Intake",
    description: "Upload and ingestion feedback",
    personas: ["maria"],
  },
  {
    path: "/runs",
    label: "Agent Run",
    description: "Workflow progress and node state",
    personas: ["maria", "dan", "platform"],
  },
  {
    path: "/audit",
    label: "Audit & Provenance",
    description: "Trace and immutable decision history",
    personas: ["priya"],
  },
  {
    path: "/evals",
    label: "Evals",
    description: "Experiment reports and quality metrics",
    personas: ["priya", "platform"],
  },
];

export function routesFor(persona: PersonaId): readonly AppRoute[] {
  return appRoutes.filter((route) => route.personas.includes(persona));
}

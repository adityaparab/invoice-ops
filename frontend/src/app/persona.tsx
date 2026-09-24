import { createContext, useCallback, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";

export const personaIds = ["maria", "dan", "priya", "platform"] as const;
export type PersonaId = (typeof personaIds)[number];

export const personaLabels: Record<PersonaId, string> = {
  maria: "Maria · AP Analyst",
  dan: "Dan · Procurement Manager",
  priya: "Priya · Auditor",
  platform: "Platform Engineer",
};

export function isPersona(value: string): value is PersonaId {
  return personaIds.some((id) => id === value);
}

interface PersonaState {
  persona: PersonaId;
  token: string;
  choosePersona: (persona: PersonaId) => void;
  setToken: (token: string) => void;
}

const PersonaContext = createContext<PersonaState | null>(null);

export function PersonaProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [persona, setPersona] = useState<PersonaId>("maria");
  const [tokens, setTokens] = useState<Partial<Record<PersonaId, string>>>({});

  const choosePersona = useCallback(
    (next: PersonaId) => {
      if (next !== persona) {
        queryClient.clear();
        setPersona(next);
      }
    },
    [persona, queryClient],
  );
  const setToken = useCallback(
    (value: string) => {
      queryClient.clear();
      setTokens((previous) => ({ ...previous, [persona]: value }));
    },
    [persona, queryClient],
  );
  const value = useMemo(
    () => ({ persona, token: tokens[persona] ?? "", choosePersona, setToken }),
    [persona, tokens, choosePersona, setToken],
  );
  return <PersonaContext.Provider value={value}>{children}</PersonaContext.Provider>;
}

export function usePersona(): PersonaState {
  const value = useContext(PersonaContext);
  if (value === null) throw new Error("PersonaProvider is missing");
  return value;
}

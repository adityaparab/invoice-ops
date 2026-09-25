import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { currentUser, login, logout } from "../api/auth";
import { sessionUserSchema } from "../schemas/auth";
import type { SessionUser } from "../schemas/auth";

export const personaIds = ["maria", "dan", "priya", "platform"] as const;
export type PersonaId = (typeof personaIds)[number];

export const personaLabels: Record<PersonaId, string> = {
  maria: "Maria · AP Analyst",
  dan: "Dan · Procurement Manager",
  priya: "Priya · Auditor",
  platform: "Platform Engineer",
};

const personaForRole = {
  ANALYST: "maria", MANAGER: "dan", AUDITOR: "priya", PLATFORM: "platform",
} as const;
const STORAGE_KEY = "invoiceops-session";

interface SessionState { token: string; user: SessionUser }

function storedSession(): SessionState | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null || !("token" in parsed) ||
      !("user" in parsed) || typeof parsed.token !== "string" ||
      !parsed.token.startsWith("io_")) return null;
    const user = sessionUserSchema.safeParse(parsed.user);
    return user.success ? { token: parsed.token, user: user.data } : null;
  } catch { return null; }
}

interface PersonaState {
  persona: PersonaId;
  token: string;
  email: string;
  authenticated: boolean;
  loading: boolean;
  signIn: (email: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
}

const PersonaContext = createContext<PersonaState | null>(null);

export function PersonaProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [session, setSession] = useState<SessionState | null>(storedSession);
  const [loading, setLoading] = useState(session !== null);

  const clearSession = useCallback(() => {
    sessionStorage.removeItem(STORAGE_KEY);
    queryClient.clear();
    setSession(null);
    setLoading(false);
  }, [queryClient]);

  useEffect(() => {
    const saved = storedSession();
    if (!saved) return;
    let active = true;
    void currentUser(saved.token).then((user) => {
      if (active) {
        const restored = { token: saved.token, user };
        sessionStorage.setItem(STORAGE_KEY, JSON.stringify(restored));
        setSession(restored);
        setLoading(false);
      }
    }).catch(() => { if (active) clearSession(); });
    return () => { active = false; };
  }, [clearSession]);

  useEffect(() => {
    const expire = (event: Event) => {
      if ((event as CustomEvent<string>).detail === session?.token) clearSession();
    };
    window.addEventListener("invoiceops:unauthorized", expire);
    return () => window.removeEventListener("invoiceops:unauthorized", expire);
  }, [clearSession, session?.token]);

  const signIn = useCallback(async (email: string, password: string) => {
    const result = await login(email, password);
    const next = { token: result.token, user: { email: result.email, role: result.role } };
    queryClient.clear();
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    setSession(next);
  }, [queryClient]);

  const signOut = useCallback(async () => {
    const token = session?.token;
    clearSession();
    if (token) await logout(token).catch(() => undefined);
  }, [session, clearSession]);

  const value = useMemo<PersonaState>(() => ({
    persona: session ? personaForRole[session.user.role] : "maria",
    token: session?.token ?? "",
    email: session?.user.email ?? "",
    authenticated: session !== null,
    loading,
    signIn,
    signOut,
  }), [session, loading, signIn, signOut]);
  return <PersonaContext.Provider value={value}>{children}</PersonaContext.Provider>;
}

export function usePersona(): PersonaState {
  const value = useContext(PersonaContext);
  if (value === null) throw new Error("PersonaProvider is missing");
  return value;
}

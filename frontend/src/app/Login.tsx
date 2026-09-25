import { Alert, Button, PasswordInput, Text, TextInput, Title } from "@mantine/core";
import { useState } from "react";
import { usePersona } from "./persona";
import styles from "./Login.module.css";

export function Login() {
  const { signIn } = usePersona();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setError(null);
    try { await signIn(email, password); }
    catch { setError("Invalid credentials or login is unavailable."); }
    finally { setPending(false); }
  }

  return (
    <main className={styles.page}>
      <form className={styles.card} onSubmit={(event) => { void submit(event); }}>
        <Text className={styles.mark}>IO</Text>
        <Title order={1}>Sign in to InvoiceOps</Title>
        <Text className={styles.description}>Use the account configured for your role.</Text>
        <TextInput label="Email" type="email" autoComplete="username" value={email}
          onChange={(event) => setEmail(event.currentTarget.value)} required disabled={pending} />
        <PasswordInput label="Password" autoComplete="current-password" value={password}
          onChange={(event) => setPassword(event.currentTarget.value)} required disabled={pending} />
        {error && <Alert title="Sign in failed">{error}</Alert>}
        <Button type="submit" loading={pending}>Sign in</Button>
      </form>
    </main>
  );
}

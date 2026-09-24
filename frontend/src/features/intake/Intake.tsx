import { Alert, Button, FileInput, PasswordInput, Progress, Text, Title } from "@mantine/core";
import { useState } from "react";
import { Link } from "react-router";
import { ApiError } from "../../api/client";
import { usePersona } from "../../app/persona";
import { useIntakeStatus, useUploadInvoice } from "./useIntake";
import styles from "./Intake.module.css";

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.status > 0 ? `${error.status}: ${error.message}` : error.message;
  }
  return error instanceof Error ? error.message : "The upload could not be completed.";
}

export function Intake() {
  const { token: analystToken } = usePersona();
  const [serviceToken, setServiceToken] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const { mutation, progress, cancel } = useUploadInvoice();
  const result = mutation.data;
  const status = useIntakeStatus(analystToken, result?.invoice_id ?? null);

  return (
    <div className={styles.screen}>
      <div>
        <Text className={styles.eyebrow}>Document intake</Text>
        <Title order={2}>Intake</Title>
        <Text className={styles.muted}>Upload a synthetic PDF, PNG, or JPEG invoice for audited processing.</Text>
      </div>
      <section className={styles.card} aria-labelledby="upload-title">
        <Title order={3} id="upload-title">Upload invoice</Title>
        <Text className={styles.muted}>
          Uploads use the separate service token. Enter Maria’s persona token in the sidebar to
          read the invoice’s current status after submission.
        </Text>
        <form className={styles.form} onSubmit={(event) => {
          event.preventDefault();
          if (file && serviceToken) mutation.mutate({ file, token: serviceToken });
        }}>
          <PasswordInput
            label="Upload service token"
            description="Kept in this tab’s memory only"
            value={serviceToken}
            onChange={(event) => setServiceToken(event.currentTarget.value)}
            disabled={mutation.isPending}
            required
          />
          <FileInput
            label="Invoice document"
            description="PDF, PNG, or JPEG"
            placeholder="Choose a synthetic invoice"
            accept="application/pdf,image/png,image/jpeg"
            value={file}
            onChange={(value) => { setFile(value); mutation.reset(); }}
            disabled={mutation.isPending}
            clearable
            required
          />
          {mutation.isPending && (
            <div className={styles.progressArea}>
              <Text>{progress === null || progress < 100
                ? `Uploading${progress === null ? "…" : ` ${progress}%`}`
                : "Upload sent. Waiting for the API…"}</Text>
              <Progress className={styles.progress} value={progress ?? 0} aria-label="Upload progress" />
              <Button type="button" onClick={cancel}>Cancel upload</Button>
            </div>
          )}
          {mutation.isError && <Alert title="Upload rejected">{errorMessage(mutation.error)}</Alert>}
          <Button type="submit" disabled={!file || !serviceToken || mutation.isPending}>
            {mutation.isPending ? "Uploading…" : "Upload invoice"}
          </Button>
        </form>
      </section>
      {result && (
        <section className={styles.card} aria-labelledby="result-title">
          <Title order={3} id="result-title">
            {result.duplicate ? "Duplicate rejected" : "Invoice accepted"}
          </Title>
          <Text>{result.duplicate
            ? "This content was already ingested. The IDs below belong to the original invoice."
            : "The invoice was stored and queued for processing."}</Text>
          <dl className={styles.identifiers}>
            <div><dt>Invoice ID</dt><dd>{result.invoice_id}</dd></div>
            <div><dt>Run ID</dt><dd>{result.run_id}</dd></div>
            <div><dt>Ingest response</dt><dd>{result.status}</dd></div>
          </dl>
          {analystToken.length === 0 && (
            <Text>Enter Maria’s persona token in the sidebar to load the current status.</Text>
          )}
          {status.isPending && analystToken.length > 0 && <Text>Loading current status…</Text>}
          {status.isError && <Alert title="Status unavailable">{errorMessage(status.error)}</Alert>}
          {status.data && (
            <div className={styles.status}>
              <Text>Invoice: {status.data.invoice.status}</Text>
              <Text>Run: {status.data.invoice.run_status}</Text>
              {status.data.exception && <Text>Exception: {status.data.exception.exception_type}</Text>}
            </div>
          )}
          <Link className={styles.queueLink} to="/queue">Open Exception Review</Link>
        </section>
      )}
    </div>
  );
}

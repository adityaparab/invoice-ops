import { flexRender, getCoreRowModel, getFilteredRowModel, getSortedRowModel, useReactTable } from "@tanstack/react-table";
import { Alert, Badge, Button, Select, Switch, Table, Text, TextInput, Title, UnstyledButton } from "@mantine/core";
import { useMemo, useState } from "react";
import { Link } from "react-router";
import { createColumnHelper } from "@tanstack/react-table";
import { usePersona } from "../../app/persona";
import type { InvoiceFilters } from "../../api/invoices";
import type { InvoiceDetail, InvoiceSummary } from "../../schemas/invoice_read";
import { DecisionForm } from "./DecisionForm";
import { readReviewEvidence, threeWayRows } from "./evidence";
import { useInvoiceDetail, useInvoiceQueue } from "./useReview";
import styles from "./ExceptionReview.module.css";

const column = createColumnHelper<InvoiceSummary>();

function priorityLabel(priority: number | null): string {
  return priority === null ? "—" : ["Routine", "Low", "High", "Critical"][priority] ?? "—";
}

export function slaLabel(dueAt: string | null, now = Date.now()): string {
  if (dueAt === null) return "—";
  const hours = Math.max(1, Math.ceil(Math.abs(Date.parse(dueAt) - now) / 3_600_000));
  return Date.parse(dueAt) < now ? `${hours}h overdue` : `Due in ${hours}h`;
}

function valueText(value: string | null | undefined): string {
  return value ?? "—";
}

function QueueFilters({ filters, onChange }: {
  filters: InvoiceFilters;
  onChange: (filters: InvoiceFilters) => void;
}) {
  return (
    <div className={styles.filters}>
      <Select
        label="Invoice status"
        data={["NEEDS_REVIEW", "PROCESSING", "APPROVED", "RETURNED", "FAILED"].map((value) => ({ value, label: value.replaceAll("_", " ") }))}
        value={filters.status ?? null}
        onChange={(status) => onChange({ ...filters, status: status as InvoiceFilters["status"] ?? undefined })}
        clearable
      />
      <Select
        label="Source"
        data={[{ value: "UPLOAD", label: "Upload" }, { value: "EMAIL", label: "Email" }]}
        value={filters.source ?? null}
        onChange={(source) => onChange({ ...filters, source: source as InvoiceFilters["source"] ?? undefined })}
        clearable
      />
      <Select
        label="Minimum priority"
        data={[0, 1, 2, 3].map((priority) => ({ value: String(priority), label: priorityLabel(priority) }))}
        value={filters.min_priority === undefined ? null : String(filters.min_priority)}
        onChange={(priority) => onChange({ ...filters, min_priority: priority === null ? undefined : Number(priority) })}
        clearable
      />
      <Switch
        label="Exceptions only"
        checked={filters.exception_only ?? false}
        onChange={(event) => onChange({ ...filters, exception_only: event.currentTarget.checked })}
      />
    </div>
  );
}

function Queue({ token, onSelect, selectedId }: {
  token: string;
  onSelect: (invoiceId: string) => void;
  selectedId: string | null;
}) {
  const [filters, setFilters] = useState<InvoiceFilters>({ exception_only: true, limit: 50 });
  const [search, setSearch] = useState("");
  const queue = useInvoiceQueue(token, filters);
  const rows = useMemo(() => queue.data?.pages.flatMap((page) => page.items) ?? [], [queue.data]);
  const columns = useMemo(() => [
    column.accessor("invoice_number", {
      header: "Invoice",
      cell: ({ row }) => (
        <UnstyledButton className={styles.invoiceLink} onClick={() => onSelect(row.original.id)}>
          {valueText(row.original.invoice_number)}
        </UnstyledButton>
      ),
    }),
    column.accessor("vendor_name", { header: "Vendor", cell: (info) => valueText(info.getValue()) }),
    column.accessor("total_amount", {
      header: "Amount",
      enableSorting: false,
      cell: ({ row }) => `${valueText(row.original.total_amount)} ${valueText(row.original.currency)}`,
    }),
    column.accessor("exception_type", { header: "Exception", cell: (info) => valueText(info.getValue()) }),
    column.accessor("exception_priority", {
      header: "Priority", cell: (info) => priorityLabel(info.getValue()),
    }),
    column.accessor("exception_sla_due_at", {
      header: "SLA", cell: (info) => slaLabel(info.getValue()),
    }),
    column.accessor("status", { header: "Status" }),
  ], [onSelect]);
  const table = useReactTable({
    data: rows,
    columns,
    getCoreRowModel: getCoreRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getSortedRowModel: getSortedRowModel(),
    state: { globalFilter: search },
    onGlobalFilterChange: setSearch,
    globalFilterFn: (row, _columnId, value: string) =>
      [row.original.vendor_name, row.original.invoice_number, row.original.po_number, row.original.exception_type]
        .some((field) => field?.toLowerCase().includes(value.toLowerCase())),
  });

  return (
    <section className={styles.card} aria-labelledby="queue-title">
      <div className={styles.sectionHeader}>
        <div>
          <Title order={3} id="queue-title">Invoice queue</Title>
          <Text className={styles.muted}>Sorted and searched across loaded rows. Filters apply to the full queue.</Text>
        </div>
        <Badge>{rows.length} loaded</Badge>
      </div>
      <QueueFilters filters={filters} onChange={setFilters} />
      <TextInput
        label="Search loaded rows"
        placeholder="Vendor, invoice, PO, or exception"
        value={search}
        onChange={(event) => setSearch(event.currentTarget.value)}
      />
      {queue.isError && <Alert title="Queue unavailable">{queue.error.message}</Alert>}
      {queue.isPending && <Text>Loading queue…</Text>}
      {!queue.isPending && !queue.isError && (
        <div className={styles.tableWrap}>
          <Table className={styles.table}>
            <Table.Thead>
              {table.getHeaderGroups().map((group) => (
                <Table.Tr key={group.id}>
                  {group.headers.map((header) => (
                    <Table.Th key={header.id}>
                      {header.column.getCanSort() ? (
                        <UnstyledButton className={styles.sortButton} onClick={header.column.getToggleSortingHandler()}>
                          {flexRender(header.column.columnDef.header, header.getContext())}
                          {header.column.getIsSorted() === "asc" ? " ↑" : header.column.getIsSorted() === "desc" ? " ↓" : ""}
                        </UnstyledButton>
                      ) : flexRender(header.column.columnDef.header, header.getContext())}
                    </Table.Th>
                  ))}
                </Table.Tr>
              ))}
            </Table.Thead>
            <Table.Tbody>
              {table.getRowModel().rows.map((row) => (
                <Table.Tr key={row.id} className={row.original.id === selectedId ? styles.selectedRow : undefined}>
                  {row.getVisibleCells().map((cell) => (
                    <Table.Td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</Table.Td>
                  ))}
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
          {table.getRowModel().rows.length === 0 && <Text className={styles.empty}>No matching invoices.</Text>}
        </div>
      )}
      {queue.hasNextPage && (
        <Button onClick={() => void queue.fetchNextPage()} disabled={queue.isFetchingNextPage}>
          {queue.isFetchingNextPage ? "Loading…" : "Load more"}
        </Button>
      )}
    </section>
  );
}

function DetailContent({ detail, token }: { detail: InvoiceDetail; token: string }) {
  const { persona } = usePersona();
  const evidence = readReviewEvidence(detail);
  const rows = threeWayRows(evidence);
  const extraction = evidence.extraction;
  const fields = extraction ? [
    ["Vendor", extraction.vendor_name],
    ["Vendor tax ID", extraction.vendor_tax_id],
    ["Bank account", { ...extraction.bank_account_iban,
      value: extraction.bank_account_iban.value === null
        ? null : `•••• ${extraction.bank_account_iban.value.slice(-4)}` }],
    ["Invoice number", extraction.invoice_number],
    ["PO number", extraction.po_number],
    ["Currency", extraction.currency],
    ["Invoice date", extraction.invoice_date],
    ["Due date", extraction.due_date],
    ["Subtotal", extraction.subtotal],
    ["Tax", extraction.tax_amount],
    ["Total", extraction.total_amount],
  ] as const : [];
  const draft = evidence.triage?.draft;
  return (
    <div className={styles.detailSections}>
      <section className={styles.card} aria-labelledby="match-title">
        <div className={styles.sectionHeader}>
          <Title order={3} id="match-title">Three-way match</Title>
          <Badge>{evidence.match?.status ?? "No match evidence"}</Badge>
        </div>
        {rows.length > 0 ? (
          <div className={styles.tableWrap}>
            <Table className={styles.table}>
              <Table.Thead><Table.Tr>
                <Table.Th>Field</Table.Th><Table.Th>Invoice</Table.Th>
                <Table.Th>Purchase order</Table.Th><Table.Th>Goods receipts</Table.Th><Table.Th>Result</Table.Th>
              </Table.Tr></Table.Thead>
              <Table.Tbody>{rows.map((row) => (
                <Table.Tr key={row.label}>
                  <Table.Td>{row.label}</Table.Td><Table.Td>{row.invoice}</Table.Td>
                  <Table.Td>{row.purchaseOrder}</Table.Td><Table.Td>{row.goodsReceipt}</Table.Td>
                  <Table.Td>{row.status}</Table.Td>
                </Table.Tr>
              ))}</Table.Tbody>
            </Table>
          </div>
        ) : <Text>Three-way comparison evidence is unavailable for this run.</Text>}
      </section>
      <section className={styles.card} aria-labelledby="extraction-title">
        <Title order={3} id="extraction-title">Extracted fields</Title>
        {fields.length > 0 ? (
          <>
            <div className={styles.fieldGrid}>
              {fields.map(([label, field]) => (
                <div key={label} className={styles.field}>
                  <Text className={styles.fieldLabel}>{label}</Text>
                  <Text>{valueText(field.value)}</Text>
                  <Text className={styles.muted}>Confidence {Math.round(Number(field.confidence) * 100)}%</Text>
                </div>
              ))}
            </div>
            {extraction && extraction.line_items.length > 0 && (
              <div className={styles.tableWrap}>
                <Table className={styles.table}>
                  <Table.Thead><Table.Tr>
                    <Table.Th>Line</Table.Th><Table.Th>Description</Table.Th>
                    <Table.Th>Quantity</Table.Th><Table.Th>Unit price</Table.Th>
                    <Table.Th>Tax rate</Table.Th><Table.Th>Line total</Table.Th>
                  </Table.Tr></Table.Thead>
                  <Table.Tbody>{extraction.line_items.map((line, index) => (
                    <Table.Tr key={index}>
                      <Table.Td>{index + 1}</Table.Td>
                      {[line.description, line.quantity, line.unit_price, line.tax_rate, line.line_total].map((field, fieldIndex) => (
                        <Table.Td key={fieldIndex}>
                          {valueText(field.value)} <span className={styles.muted}>({Math.round(Number(field.confidence) * 100)}%)</span>
                        </Table.Td>
                      ))}
                    </Table.Tr>
                  ))}</Table.Tbody>
                </Table>
              </div>
            )}
          </>
        ) : <Text>Validated extraction fields are unavailable for this run.</Text>}
      </section>
      <section className={styles.card} aria-labelledby="agent-title">
        <Title order={3} id="agent-title">Agent findings and recommendation</Title>
        {draft ? (
          <>
            <Badge>{draft.recommended_action}</Badge>
            <Text>{draft.summary}</Text>
            <Text>{draft.rationale}</Text>
            <ul className={styles.findings}>
              {draft.evidence_refs.map((ref) => {
                const fact = evidence.facts.find((item) => item.ref === ref);
                return <li key={ref}><strong>{ref}</strong>: {fact?.detail ?? "Evidence reference unavailable"}</li>;
              })}
            </ul>
          </>
        ) : <Text>No cited agent draft is available. Human review remains required.</Text>}
      </section>
      <DecisionForm key={`${detail.invoice.id}:${detail.pending_proposal?.id ?? "open"}:${persona}`} detail={detail} token={token} persona={persona} />
    </div>
  );
}

export function ExceptionReview() {
  const { token } = usePersona();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const detail = useInvoiceDetail(token, selectedId);
  return (
    <div className={styles.screen}>
      <div>
        <Text className={styles.eyebrow}>Human review</Text>
        <Title order={2}>Exception Review</Title>
        <Text className={styles.muted}>Inspect audited evidence and record a two-person decision.</Text>
      </div>
      {token.length > 0 && <Queue token={token} onSelect={setSelectedId} selectedId={selectedId} />}
      {selectedId && token.length > 0 && (
        <section aria-label="Invoice detail">
          {detail.isPending && <Text>Loading invoice detail…</Text>}
          {detail.isError && <Alert title="Invoice detail unavailable">{detail.error.message}</Alert>}
          {detail.data && (
            <>
              <div className={styles.detailHeader}>
                <div>
                  <Text className={styles.eyebrow}>Selected invoice</Text>
                  <Title order={2}>{valueText(detail.data.invoice.invoice_number)}</Title>
                  <Text>{valueText(detail.data.invoice.vendor_name)} · {valueText(detail.data.invoice.total_amount)} {valueText(detail.data.invoice.currency)}</Text>
                </div>
                <Badge>{detail.data.exception?.exception_type ?? detail.data.invoice.status}</Badge>
              </div>
              <Link className={styles.runLink} to={`/runs?run_id=${detail.data.invoice.run_id}`}>Follow Agent Run</Link>
              <DetailContent detail={detail.data} token={token} />
            </>
          )}
        </section>
      )}
    </div>
  );
}

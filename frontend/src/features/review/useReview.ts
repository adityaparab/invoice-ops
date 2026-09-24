import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";
import { fetchInvoiceDetail, fetchInvoicePage } from "../../api/invoices";
import { submitDecision } from "../../api/decisions";
import type { InvoiceFilters } from "../../api/invoices";
import type { DecisionRequest } from "../../schemas/decision";

export function useInvoiceQueue(token: string, filters: InvoiceFilters) {
  return useInfiniteQuery({
    queryKey: ["invoice-queue", filters],
    queryFn: ({ pageParam }) => fetchInvoicePage(token, { ...filters, cursor: pageParam ?? undefined }),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor,
    enabled: token.length > 0,
  });
}

export function useInvoiceDetail(token: string, invoiceId: string | null) {
  return useQuery({
    queryKey: ["invoice-detail", invoiceId],
    queryFn: () => fetchInvoiceDetail(token, invoiceId ?? ""),
    enabled: token.length > 0 && invoiceId !== null,
  });
}

export function useDecision(token: string, exceptionId: string, invoiceId: string) {
  const queryClient = useQueryClient();
  const retryKey = useRef<{ body: string; key: string } | null>(null);
  return useMutation({
    mutationFn: (input: DecisionRequest) => {
      const body = JSON.stringify(input);
      const key = retryKey.current?.body === body
        ? retryKey.current.key : `review-${crypto.randomUUID()}`;
      retryKey.current = { body, key };
      return submitDecision(token, exceptionId, key, input);
    },
    onSuccess: async () => {
      retryKey.current = null;
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["invoice-detail", invoiceId] }),
        queryClient.invalidateQueries({ queryKey: ["invoice-queue"] }),
      ]);
    },
  });
}

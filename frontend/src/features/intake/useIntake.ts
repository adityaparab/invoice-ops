import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { fetchInvoiceDetail } from "../../api/invoices";
import { uploadInvoice } from "../../api/upload";

export function useUploadInvoice() {
  const queryClient = useQueryClient();
  const request = useRef<XMLHttpRequest | null>(null);
  const retry = useRef<{ file: File; key: string } | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const mutation = useMutation({
    mutationFn: ({ file, token }: { file: File; token: string }) => {
      const key = retry.current?.file === file
        ? retry.current.key : `intake-${crypto.randomUUID()}`;
      retry.current = { file, key };
      return uploadInvoice(file, token, key, {
        onProgress: setProgress,
        onRequest: (value) => { request.current = value; },
      });
    },
    onMutate: () => setProgress(0),
    onSuccess: async () => {
      retry.current = null;
      await queryClient.invalidateQueries({ queryKey: ["invoice-queue"] });
    },
    onSettled: () => { request.current = null; },
  });
  return { mutation, progress, cancel: () => request.current?.abort() };
}

export function useIntakeStatus(analystToken: string, invoiceId: string | null) {
  return useQuery({
    queryKey: ["invoice-detail", invoiceId],
    queryFn: () => fetchInvoiceDetail(analystToken, invoiceId ?? ""),
    enabled: analystToken.length > 0 && invoiceId !== null,
    refetchInterval: (query) => {
      const status = query.state.data?.invoice.run_status;
      return status === "COMPLETED" || status === "FAILED" || status === "CANCELLED"
        ? false : 5_000;
    },
  });
}

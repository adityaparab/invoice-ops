import { useInfiniteQuery } from "@tanstack/react-query";
import { fetchAuditRunPage } from "../../api/audit";

export function useAuditRun(token: string, runId: string | null) {
  return useInfiniteQuery({
    queryKey: ["audit-run", runId],
    queryFn: ({ pageParam }) => fetchAuditRunPage(token, runId ?? "", pageParam),
    initialPageParam: null as number | null,
    getNextPageParam: (page) => page.next_cursor?.sequence ?? null,
    enabled: token.length > 0 && runId !== null,
  });
}

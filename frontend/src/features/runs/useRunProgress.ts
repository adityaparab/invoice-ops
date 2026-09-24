import { useQuery } from "@tanstack/react-query";
import { fetchRunProgress } from "../../api/runs";

export function useRunProgress(token: string, runId: string | null) {
  return useQuery({
    queryKey: ["run-progress", runId],
    queryFn: () => fetchRunProgress(token, runId ?? ""),
    enabled: token.length > 0 && runId !== null,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "COMPLETED" || status === "FAILED" || status === "CANCELLED"
        ? false : 2_000;
    },
  });
}

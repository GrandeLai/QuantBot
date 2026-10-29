export type QueueEntry = { id: string; study_id: string; title: string; planned: number; started: number | null;
  finished: number | null; status: string; delay_seconds: number | null; duration_seconds: number | null; error: string | null };
export type ResearchOperations = {
  as_of: number; worker_online: boolean; concurrency_limit: number; latest_success: number | null; latest_failure: number | null;
  study_counts: Record<string, number>; queue_counts: Record<string, number>; reserved_model_requests: number;
  model_usage: { decision_calls: number; reported_tokens: number | null; missing_usage_calls: number; unknown_trials: number | null; unresolved_calls: number };
  disk: { free_bytes: number; total_bytes: number }; basis: string; queue: QueueEntry[];
  studies: { id: string; title: string; status: string; conclusion: string | null; trials: number; failed_trials: number;
    data_quality: { source: string; rows: number; end: string; availability_enforced?: boolean; revision_count?: number; data_kind: string } }[];
};

export async function operationsRequest<T>(path = "/operations", body?: unknown): Promise<T> {
  const response = await fetch("/api/quantbot/runtime" + path, body === undefined ? undefined : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "研究运行请求失败");
  return result;
}

export function queueResearch(studyId: string, plannedAt: string, allowModelCalls = false) {
  return operationsRequest(`/research/${encodeURIComponent(studyId)}/queue`, {
    planned_at: new Date(plannedAt).toISOString(), allow_model_calls: allowModelCalls,
  });
}

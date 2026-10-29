export type HistoricalIdentity = { cik: string; symbol: string; start: string; end: string };
export type EventWindow = { sessions: number; entry_date: string; exit_date: string; net_return: number; benchmark_return: number; excess_return: number };
export type SECStudyReport = {
  execution_completed: boolean; research_conclusion: string; download_url: string; evidence_boundary: string;
  data_quality: { data_kind: string; availability_mode: string; accessions: number; duplicate_records: number; revision_versions: number; event_groups: number; quarter_count: number };
  summary: { sessions: number; events: number; entry_date_clusters: number; mean_return: number | null; mean_benchmark_return: number | null;
    mean_excess_return: number | null; double_cost_mean_return: number | null; equal_date_cluster_mean_excess: number | null; date_cluster_bootstrap_95: [number, number] | null }[];
  events: { symbol: string; accessions: string[]; items: string[]; entry_date: string; available_at: string; accepted_at: string; first_seen_at: string; source_url: string; windows: EventWindow[] }[];
  skipped: { reason: string; accession?: string; accessions?: string[]; sessions?: number }[];
  model_calls: number; broker_calls: number;
};

export function parseEventHorizons(value: string) {
  if (!/^\s*\d+(\s*,\s*\d+)*\s*$/.test(value)) throw new Error("窗口需填写以逗号分隔的交易日数，例如 1,5,20");
  const result = value.split(",").map(v => Number(v.trim()));
  if (result.length > 5 || result.some((v, i) => v < 1 || v > 60 || (i > 0 && v <= result[i - 1]))) throw new Error("窗口需按升序填写 1～60，不能重复，最多 5 项");
  return result;
}

export async function secStudyRequest<T>(path = "", body?: unknown): Promise<T> {
  const response = await fetch("/api/quantbot/sec-study" + path, body === undefined ? undefined : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "事件研究输入未通过验证");
  return result;
}

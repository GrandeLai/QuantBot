export type ResearchCard = { title: string; hypothesis: string; rationale: string; failure_condition: string };
export type ResearchMetrics = { return_fraction: number; max_drawdown_fraction: number; fills: number;
  execution_completed?: boolean; equity_curve?: { as_of: string; equity: number }[] };
export type ResearchSummary = { id: string; title: string; status: string; conclusion: string; created: number };
export type ResearchStudy = {
  id: string; status: string; plan_hash: string;
  plan: { card: ResearchCard; protocol: Record<string, number | number[]>; max_model_requests: number;
    candidates: { id: string; spec: { name: string; provider: string } }[];
    data_quality: { rows: number; start: string; end: string; checks: string[]; limitations: string[]; data_kind: string; availability_enforced?: boolean; revision_count?: number };
    windows: { holdout: { start: string; end: string } }; evidence_boundary: string };
  report: null | { execution_completed: boolean; research_passed: boolean; conclusion: string;
    reasons: string[]; checks?: Record<string, boolean>; error?: string;
    selected_job_id?: string; holdout?: ResearchMetrics; baseline?: ResearchMetrics;
    stress?: (ResearchMetrics & { scenario: string })[];
    placebos?: (ResearchMetrics & { seed: number })[];
    diagnostics?: { dsr: { status: string; confidence?: number; reason?: string };
      pbo: { status: string; probability?: number; reason?: string }; observations: number; trial_count: number };
    model_usage?: { provider_calls: number; reported_tokens: number | null } };
  trials: { key: string; stage: string; status: string; result: { error?: string } | null }[];
  events: { id: number; kind: string; at: number }[];
};

export const conclusionLabels: Record<string, string> = {
  not_assessed: "待研究", validated: "研究达标", rejected: "未达标", insufficient: "证据不足",
};
export const researchChecks: Record<string, string> = {
  historical_data: "使用历史行情（合成数据只验证流程）",
  execution_complete: "所有试验完整运行", unexposed_holdout: "留出区间未被已有研究使用",
  all_trials_comparable: "全部候选有完整可比记录", enough_development_observations: "开发样本数量足够",
  enough_holdout_observations: "留出样本数量足够", positive_development_return: "开发收益超过预设门槛",
  positive_holdout_return: "留出收益超过预设门槛", beats_rules: "超过规则基准及超额收益门槛",
  drawdown_within_limit: "留出回撤在上限内", stress_survives: "成本与仓位压力测试达标",
  beats_placebos: "超过全部预登记随机对照", dsr: "多重试验修正后的 Sharpe 置信度达标",
  pbo: "回测过拟合概率达标", no_complete_candidate: "没有完整运行的候选",
  interrupted: "运行中断，已保留证据", execution_error: "运行异常，详见错误记录",
};

export async function researchRequest<T>(path = "", body?: unknown): Promise<T> {
  const response = await fetch("/api/quantbot/runtime/research" + path, body === undefined ? undefined : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "研究请求未通过验证");
  return data;
}

export function runResearch(id: string, allowModelCalls = false): Promise<ResearchStudy> {
  return researchRequest(`/${encodeURIComponent(id)}/run`, { allow_model_calls: allowModelCalls });
}

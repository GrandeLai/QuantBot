/**
 * QuantBot API client — 封装所有后端接口调用.
 * 使用 Vite dev-proxy：/api/* → http://127.0.0.1:8001/*
 */

const BASE = "/api";

// ── 数据模型 ─────────────────────────────────────────────────────────────────

export interface OhlcBar {
  symbol: string;
  timeframe: string;
  timestamp: string; // ISO 8601
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface OhlcvResponse {
  symbol: string;
  timeframe: string;
  count: number;
  bars: OhlcBar[];
}

// key = indicator field name (e.g. "ema", "bb_upper"), value = aligned array
export type IndicatorSeries = Record<string, (number | null)[]>;

export interface TemplateSummary {
  id: string;
  name: string;
  description: string;
  version: string;
  default_params: Record<string, unknown>;
}

export interface StrategyMeta {
  id: string;
  name: string;
  description: string;
  tags: string[];
  params: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface StrategyRecord {
  meta: StrategyMeta;
  code: string;
}

export interface CryptoResearchWindowMetric {
  train_start: number;
  train_end: number;
  test_start: number;
  test_end: number;
  accuracy: number;
  strategy_return: number;
}

export interface CryptoResearchSummary {
  symbol: string;
  base_timeframe: string;
  higher_timeframes: string[];
  rows: number;
  dataset_version: string;
  feature_count: number;
  feature_columns: string[];
  validation_windows: number;
  window_metrics: CryptoResearchWindowMetric[];
  mean_accuracy: number;
  mean_strategy_return: number;
  latest_class_signal: number;
  latest_class_probabilities: Record<string, number>;
  feature_importance: Record<string, number>;
  reversal_probability: number;
  reversal_signal: string;
  reversal_evidence: string[];
  market_regime: string;
  recommended_strategy_ids: string[];
  recommended_timeframes: string[];
  parameter_search_ready: boolean;
}

export interface CryptoResearchOptimizationSummary {
  symbol: string;
  strategy_id: string;
  base_timeframe: string;
  higher_timeframes: string[];
  best_params: Record<string, number>;
  window_count: number;
  mean_accuracy: number;
  mean_strategy_return: number;
  max_drawdown: number;
  window_metrics: CryptoResearchWindowMetric[];
}

// ── 数据接口 ─────────────────────────────────────────────────────────────────

export async function fetchBars(
  symbol: string,
  timeframe: string,
  limit = 300,
): Promise<OhlcBar[]> {
  const url = `${BASE}/data/bars?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}&limit=${limit}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`获取K线失败 (${res.status}): ${await res.text()}`);
  const json = (await res.json()) as OhlcvResponse;
  return json.bars;
}

export async function triggerFetch(
  symbol: string,
  timeframe: string,
  start: string,
): Promise<void> {
  const res = await fetch(`${BASE}/data/fetch`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbol, timeframe, start, source: "auto" }),
  });
  if (!res.ok) throw new Error(`数据拉取失败 (${res.status})`);
}

// ── 指标接口 ─────────────────────────────────────────────────────────────────

export async function fetchBatchIndicators(
  bars: OhlcBar[],
  indicators: string[],
): Promise<Record<string, IndicatorSeries>> {
  const res = await fetch(`${BASE}/indicators/batch`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ bars, indicators }),
  });
  if (!res.ok) throw new Error(`批量指标计算失败 (${res.status})`);
  const json = (await res.json()) as { indicators: Record<string, IndicatorSeries> };
  return json.indicators;
}

// ── 策略接口 ─────────────────────────────────────────────────────────────────

export async function listTemplates(): Promise<TemplateSummary[]> {
  const res = await fetch(`${BASE}/strategies/templates`);
  if (!res.ok) throw new Error("获取模板列表失败");
  const json = (await res.json()) as { templates: TemplateSummary[] };
  return json.templates;
}

export async function getTemplateCode(templateId: string): Promise<string> {
  const res = await fetch(`${BASE}/strategies/templates/${templateId}/code`);
  if (!res.ok) throw new Error("获取模板代码失败");
  const json = (await res.json()) as { code: string };
  return json.code;
}

export async function listStrategies(): Promise<StrategyMeta[]> {
  const res = await fetch(`${BASE}/strategies`);
  if (!res.ok) throw new Error("获取策略列表失败");
  const json = (await res.json()) as { strategies: StrategyMeta[] };
  return json.strategies;
}

export async function getStrategy(id: string): Promise<StrategyRecord> {
  const res = await fetch(`${BASE}/strategies/${id}`);
  if (!res.ok) throw new Error("获取策略失败");
  return (await res.json()) as StrategyRecord;
}

export async function createStrategy(
  name: string,
  description: string,
  code: string,
): Promise<{ id: string }> {
  const res = await fetch(`${BASE}/strategies`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, description, code }),
  });
  if (!res.ok) throw new Error("创建策略失败");
  return (await res.json()) as { id: string };
}

export async function updateStrategy(
  id: string,
  updates: { name?: string; description?: string; code?: string },
): Promise<void> {
  const res = await fetch(`${BASE}/strategies/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(updates),
  });
  if (!res.ok) throw new Error("更新策略失败");
}

export async function deleteStrategy(id: string): Promise<void> {
  const res = await fetch(`${BASE}/strategies/${id}`, { method: "DELETE" });
  if (!res.ok) throw new Error("删除策略失败");
}

export async function fetchCryptoResearchSummary(symbol: string): Promise<CryptoResearchSummary> {
  const res = await fetch(`${BASE}/crypto/research/train`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      symbol,
      base_timeframe: "15m",
      higher_timeframes: ["1h", "4h", "1d", "1w"],
      limit: 180,
      validation: {
        train_size: 60,
        test_size: 20,
        step_size: 20,
        embargo_size: 2,
      },
    }),
  });
  if (!res.ok) throw new Error(`获取加密研究摘要失败 (${res.status}): ${await res.text()}`);
  return (await res.json()) as CryptoResearchSummary;
}

export async function fetchCryptoResearchLatestSummary(symbol: string): Promise<CryptoResearchSummary> {
  const res = await fetch(
    `${BASE}/crypto/research/latest?symbol=${encodeURIComponent(symbol)}&base_timeframe=15m`
  );
  if (!res.ok) throw new Error(`获取加密最新研究摘要失败 (${res.status}): ${await res.text()}`);
  return (await res.json()) as CryptoResearchSummary;
}

export async function fetchCryptoResearchOptimization(
  symbol: string,
): Promise<CryptoResearchOptimizationSummary> {
  const res = await fetch(`${BASE}/crypto/research/optimize`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      symbol,
      base_timeframe: "15m",
      higher_timeframes: ["1h", "4h", "1d", "1w"],
      limit: 180,
      param_grid: {
        fast_period: [5, 8],
        slow_period: [20, 30],
        vwap_window: [10, 20],
        trailing_stop_pct: [0.02, 0.03],
        max_hold_bars: [24, 48],
      },
    }),
  });
  if (!res.ok) throw new Error(`获取加密参数优化失败 (${res.status}): ${await res.text()}`);
  return (await res.json()) as CryptoResearchOptimizationSummary;
}

export async function fetchCryptoResearchLatestOptimization(
  symbol: string,
  strategyId: string,
): Promise<CryptoResearchOptimizationSummary> {
  const res = await fetch(
    `${BASE}/crypto/research/optimize/latest?symbol=${encodeURIComponent(symbol)}&base_timeframe=15m&strategy_id=${encodeURIComponent(strategyId)}`,
  );
  if (!res.ok) throw new Error(`获取加密最新优化结果失败 (${res.status}): ${await res.text()}`);
  return (await res.json()) as CryptoResearchOptimizationSummary;
}

// ── 因子目录 ──────────────────────────────────────────────────────────────────

export interface FactorInfo {
  name: string;
  category: string;
  source: string;
  window: number | null;
  description: string;
  range_hint: string | null;
}

export async function fetchFactorCatalog(): Promise<FactorInfo[]> {
  const res = await fetch(`${BASE}/factors/catalog`);
  if (!res.ok) throw new Error(`获取因子目录失败 (${res.status})`);
  return (await res.json()) as FactorInfo[];
}

// ── 量化研究（参数化版本） ─────────────────────────────────────────────────────

export interface QuantResearchTrainRequest {
  symbol: string;
  base_timeframe?: string;
  higher_timeframes?: string[];
  limit?: number;
  validation: {
    train_size: number;
    test_size: number;
    step_size: number;
    embargo_size?: number;
  };
}

export async function runQuantResearchTrain(
  req: QuantResearchTrainRequest,
): Promise<CryptoResearchSummary> {
  const body = {
    symbol: req.symbol,
    base_timeframe: req.base_timeframe ?? "15m",
    higher_timeframes: req.higher_timeframes ?? ["1h", "4h", "1d"],
    limit: req.limit ?? 300,
    validation: {
      train_size: req.validation.train_size,
      test_size: req.validation.test_size,
      step_size: req.validation.step_size,
      embargo_size: req.validation.embargo_size ?? 2,
    },
  };
  const res = await fetch(`${BASE}/crypto/research/train`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`训练失败 (${res.status}): ${await res.text()}`);
  return (await res.json()) as CryptoResearchSummary;
}

export interface QuantResearchOptimizeRequest {
  symbol: string;
  base_timeframe?: string;
  higher_timeframes?: string[];
  limit?: number;
  param_grid: Record<string, number[]>;
}

export async function runQuantResearchOptimize(
  req: QuantResearchOptimizeRequest,
): Promise<CryptoResearchOptimizationSummary> {
  const body = {
    symbol: req.symbol,
    base_timeframe: req.base_timeframe ?? "15m",
    higher_timeframes: req.higher_timeframes ?? ["1h", "4h", "1d"],
    limit: req.limit ?? 300,
    param_grid: req.param_grid,
  };
  const res = await fetch(`${BASE}/crypto/research/optimize`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`优化失败 (${res.status}): ${await res.text()}`);
  return (await res.json()) as CryptoResearchOptimizationSummary;
}

// ── AutoPilot Pipeline ──────────────────────────────────────────────────────

export interface PipelineRunRequest {
  symbol: string;
  timeframe: string;
  start_date: string;
  end_date: string;
  initial_cash: number;
}

export interface PipelineStepState {
  name: string;
  label: string;
  status: "pending" | "running" | "done" | "error" | "skipped";
  message: string;
}

export interface PipelineWindowMetric {
  window: number;
  accuracy: number;
  strategy_return: number;
  train_size: number;
  test_size: number;
}

export interface PipelineResult {
  feature_count?: number;
  validation_windows?: number;
  mean_accuracy?: number;
  mean_strategy_return?: number;
  latest_class_signal?: number;
  feature_importance?: Record<string, number>;
  window_metrics?: PipelineWindowMetric[];
  market_regime?: string;
  reversal_probability?: number;
  reversal_signal?: string;
  reversal_evidence?: string[];
  recommended_strategy_ids?: string[];
  signal_action?: string;
  signal_confidence?: number;
  signal_published?: boolean;
}

export interface PipelineJobState {
  job_id: string;
  status: "pending" | "running" | "done" | "error";
  current_step: string;
  steps: PipelineStepState[];
  error: string;
  result: PipelineResult;
  created_at: string;
  finished_at: string;
}

export async function startPipeline(req: PipelineRunRequest): Promise<{ job_id: string }> {
  const r = await fetch("/api/pipeline/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json() as Promise<{ job_id: string }>;
}

export async function getPipelineJob(jobId: string): Promise<PipelineJobState> {
  const r = await fetch(`/api/pipeline/jobs/${jobId}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json() as Promise<PipelineJobState>;
}

// ── Risk Engine（phaseF.1.5）─────────────────────────────────────────────────

export type VolRegime = "low" | "normal" | "high" | "crisis";
export type DecayAlertLevel = "green" | "yellow" | "red";
export type VarMethod = "historical" | "parametric" | "both";

export interface VolTargetResult {
  realized_vol: number;
  target_vol: number;
  scale_factor: number;
  regime: VolRegime;
}

export interface SharpeDecayResult {
  recent_sharpe: number;
  baseline_mean: number;
  baseline_std: number;
  z_score: number;
  alert_level: DecayAlertLevel;
  n_baseline_samples: number;
}

export interface VarResult {
  n_samples: number;
  worst_loss: number;
  method: VarMethod;
  var_95?: number;
  var_99?: number;
  cvar_95?: number;
  cvar_99?: number;
  parametric_var_95?: number;
  parametric_var_99?: number;
}

export interface RiskSummaryResult {
  n_samples: number;
  vol_target: VolTargetResult;
  sharpe_decay: SharpeDecayResult | null;
  var: VarResult;
}

export interface KellyResult {
  full_kelly: number;
  fractional_kelly: number;
  capped_kelly: number;
  mode: "binary" | "returns";
  fraction: number;
  cap: number;
}

async function postRisk<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`${BASE}/risk/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text) as { detail?: string };
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<T>;
}

export function fetchRiskSummary(returns: number[]): Promise<RiskSummaryResult> {
  return postRisk<RiskSummaryResult>("summary", { returns });
}

export function fetchRiskKellyBinary(
  winRate: number,
  payoffRatio: number,
  fraction = 0.25,
  cap = 0.25,
): Promise<KellyResult> {
  return postRisk<KellyResult>("kelly", {
    mode: "binary",
    win_rate: winRate,
    payoff_ratio: payoffRatio,
    fraction,
    cap,
  });
}

export function fetchRiskKellyFromReturns(
  returns: number[],
  fraction = 0.25,
  cap = 0.25,
): Promise<KellyResult> {
  return postRisk<KellyResult>("kelly", {
    mode: "returns",
    returns,
    fraction,
    cap,
  });
}

// ── Crypto Derivatives（phaseF.1.13）────────────────────────────────────────

export interface FundingRateLite {
  exchange: "binance" | "okx";
  symbol: string;
  raw_symbol: string;
  funding_rate: number;
  next_funding_time: string | null;
  timestamp: string;
}

export interface OpenInterestLite {
  exchange: "binance" | "okx";
  symbol: string;
  raw_symbol: string;
  open_interest: number;
  open_interest_value: number | null;
  timestamp: string;
}

export interface CryptoDerivsSnapshot {
  asset: string;
  timestamp: string;
  funding: { binance: FundingRateLite | null; okx: FundingRateLite | null };
  open_interest: { binance: OpenInterestLite | null; okx: OpenInterestLite | null };
  errors: Record<string, string>;
}

export interface FundingStats {
  mean: number;
  std: number;
  p5: number;
  p25: number;
  p50: number;
  p75: number;
  p95: number;
}

export interface FundingExtremeSignal {
  z_score: number;
  percentile: number;
  signal: "contrarian_short" | "contrarian_long" | "neutral";
}

export interface FundingStatsResult {
  stats: FundingStats;
  n_samples: number;
  signal: FundingExtremeSignal | null;
}

export interface ETFFlowExtremeSignal {
  z_score: number;
  percentile: number;
  signal: "large_inflow" | "large_outflow" | "neutral";
}

export interface ETFFlowStatsResult {
  stats: FundingStats & { n_samples: number };
  signal: ETFFlowExtremeSignal | null;
}

async function postCryptoDerivs<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`${BASE}/crypto-derivs/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text) as { detail?: string };
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<T>;
}

export function fetchCryptoDerivsSnapshot(asset = "BTC"): Promise<CryptoDerivsSnapshot> {
  return postCryptoDerivs<CryptoDerivsSnapshot>("snapshot", { asset });
}

export function fetchFundingStats(
  history: number[],
  current: number | null = null,
  zThreshold = 2.0,
): Promise<FundingStatsResult> {
  return postCryptoDerivs<FundingStatsResult>("funding-stats", {
    history,
    current,
    z_threshold: zThreshold,
  });
}

export function fetchETFFlowStats(
  history: number[],
  current: number | null = null,
  zThreshold = 2.0,
): Promise<ETFFlowStatsResult> {
  return postCryptoDerivs<ETFFlowStatsResult>("etf-flow-stats", {
    history,
    current,
    z_threshold: zThreshold,
  });
}

// ── GEX (Dealer Gamma Exposure) ──────────────────────────────────────────────

export interface GEXByStrike {
  strike: number;
  call_oi: number;
  put_oi: number;
  call_gex: number;
  put_gex: number;
  net_gex: number;
  gamma: number;
  dte: number;
}

export interface GEXSnapshot {
  ticker: string;
  spot: number;
  snapshot_time: string;
  gex_by_strike: GEXByStrike[];
  net_gex_total: number;
  gamma_flip_level: number | null;
  major_magnet: number | null;
  high_vol_trigger: number | null;
}

export interface GEXLevels {
  ticker: string;
  spot: number;
  snapshot_time: string;
  net_gex_total: number;
  gamma_flip_level: number | null;
  major_magnet: number | null;
  high_vol_trigger: number | null;
}

async function getGex<T>(path: string, params: Record<string, string | number>): Promise<T> {
  const qs = new URLSearchParams(
    Object.entries(params).map(([k, v]) => [k, String(v)]),
  ).toString();
  const r = await fetch(`${BASE}/options/gex/${path}?${qs}`);
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text) as { detail?: string };
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<T>;
}

export function fetchGEXSnapshot(
  ticker = "SPY",
  maxDte = 45,
  minOi = 10,
): Promise<GEXSnapshot> {
  return getGex<GEXSnapshot>("snapshot", { ticker, max_dte: maxDte, min_oi: minOi });
}

export function fetchGEXLevels(
  ticker = "SPY",
  maxDte = 45,
  minOi = 10,
): Promise<GEXLevels> {
  return getGex<GEXLevels>("levels", { ticker, max_dte: maxDte, min_oi: minOi });
}

// ── SEC 事件流（8-K diff + Form 4 集群）────────────────────────────────────

export interface SECEightKItem {
  item_number: string;
  item_title: string;
  text_snippet: string;
}

export interface SECParagraphDiff {
  diff_type: "added" | "removed" | "modified" | "unchanged";
  old_text: string | null;
  new_text: string | null;
  similarity: number;
}

export interface SECItemDiff {
  item_number: string;
  item_title: string;
  has_material_change: boolean;
  change_score: number;
  paragraphs: SECParagraphDiff[];
}

export interface SECDiffSummary {
  has_diff: boolean;
  has_material_change?: boolean;
  overall_change_score?: number;
  changed_items?: string[];
}

export interface SECInsiderTransaction {
  insider_name: string;
  insider_title: string;
  transaction_date: string;
  transaction_type: string;
  shares: number;
  price_per_share: number;
  total_value: number;
  is_10b5_1_plan: boolean;
}

export interface SECInsiderCluster {
  window_start: string;
  window_end: string;
  insider_count: number;
  total_value: number;
  avg_price: number;
  signal_strength: number;
  key_roles: string[];
  transactions: SECInsiderTransaction[];
}

export interface SECSummary {
  ticker: string;
  generated_at: string;
  latest_8k: {
    filed_date?: string;
    accession_number?: string;
    items?: SECEightKItem[];
  };
  "8k_diff": SECDiffSummary;
  insider_clusters: SECInsiderCluster[];
}

export async function fetchSECSummary(ticker: string): Promise<SECSummary> {
  const r = await fetch(`${BASE}/sec/summary?ticker=${encodeURIComponent(ticker)}`);
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text) as { detail?: string };
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<SECSummary>;
}

// ---------------------------------------------------------------------------
// TLH（税务亏损收割）类型定义（Phase F.3）
// ---------------------------------------------------------------------------

export interface TaxLotInput {
  ticker: string;
  quantity: number;
  cost_basis: number;
  acquisition_date: string; // "YYYY-MM-DD"
  lot_id?: string;
}

export interface TLHCandidate {
  ticker: string;
  lot_id: string;
  quantity: number;
  cost_basis: number;
  acquisition_date: string;
  current_price: number;
  unrealized_pnl: number;
  unrealized_pnl_pct: number;
  holding_days: number;
  is_long_term: boolean;
  replacement_tickers: string[];
  wash_sale_risk: boolean;
}

export interface TLHScanResponse {
  candidates: TLHCandidate[];
  estimated_tax_saving: number;
  generated_at: string;
}

export interface TLHReplacementResponse {
  ticker: string;
  replacements: string[];
}

export async function fetchTLHScan(
  lots: TaxLotInput[],
  current_prices: Record<string, number>,
  recent_purchases?: Record<string, string>,
): Promise<TLHScanResponse> {
  const r = await fetch(`${BASE}/tlh/scan`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lots, current_prices, recent_purchases: recent_purchases ?? {} }),
  });
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text) as { detail?: string };
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<TLHScanResponse>;
}

// ---------------------------------------------------------------------------
// 基本面信号类型（Phase F.4 — PEAD + Piotroski）
// ---------------------------------------------------------------------------

export interface EarningsSurprise {
  ticker: string;
  quarter: string;
  eps_actual: number;
  eps_estimate: number;
  eps_difference: number;
  surprise_pct: number;
}

export type SurpriseMagnitude =
  | "large_beat"
  | "beat"
  | "inline"
  | "miss"
  | "large_miss";

export interface PEADSignal {
  ticker: string;
  surprise_magnitude: SurpriseMagnitude;
  signal_strength: number;
  historical_drift_7d: number | null;
  historical_drift_30d: number | null;
  historical_drift_60d: number | null;
  latest_surprise: EarningsSurprise;
}

export interface PiotroskiScore {
  ticker: string;
  score: number;
  grade: "strong" | "moderate" | "weak";
  signals: Record<string, boolean>;
  as_of_date: string;
  interpretation: string;
}

export interface FundamentalSummary {
  ticker: string;
  pead: PEADSignal | null;
  piotroski: PiotroskiScore | null;
}

export async function fetchFundamentalSummary(
  ticker: string,
): Promise<FundamentalSummary> {
  const r = await fetch(
    `${BASE}/fundamental/summary?ticker=${encodeURIComponent(ticker)}`,
  );
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text) as { detail?: string };
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<FundamentalSummary>;
}

// ---------------------------------------------------------------------------
// Quant Signals — Beneish M-Score + Russell Rebalancing Preview (Phase F.5)
// ---------------------------------------------------------------------------

export interface BeneishMScoreData {
  ticker: string;
  m_score: number;
  risk_level: "safe" | "grey" | "manipulator";
  ratios: Record<string, number>;
  interpretation: string;
  as_of_date: string;
}

export interface RussellMembershipData {
  ticker: string;
  market_cap_usd: number;
  estimated_rank: number | null;
  current_index:
    | "Russell 1000"
    | "Russell 2000"
    | "Outside Russell 3000"
    | "Unknown";
  proximity_score: number;
  rebalance_signal:
    | "likely_add_1000"
    | "likely_drop_1000"
    | "likely_add_2000"
    | "likely_drop_2000"
    | "stable"
    | "unknown";
}

export type SloanGrade =
  | "low_accrual"
  | "normal"
  | "elevated_accrual"
  | "high_accrual";

export interface SloanAccrualsData {
  ticker: string;
  accrual_ratio: number;
  grade: SloanGrade;
  net_income: number;
  operating_cash_flow: number;
  avg_total_assets: number;
  interpretation: string;
  as_of_date: string;
}

export interface PiotroskiCriteriaData {
  // Profitability
  roa_positive: boolean;
  cfo_positive: boolean;
  roa_improving: boolean;
  accruals_ok: boolean;
  // Leverage / Liquidity
  leverage_ok: boolean;
  liquidity_ok: boolean;
  no_dilution: boolean;
  // Operating Efficiency
  margin_ok: boolean;
  turnover_ok: boolean;
}

export type PiotroskiGrade = "strong" | "neutral" | "weak";

export interface PiotroskiScoreData {
  ticker: string;
  f_score: number;   // 0-9
  grade: PiotroskiGrade;
  criteria: PiotroskiCriteriaData;
  interpretation: string;
  as_of_date: string;
}

export interface QuantSignalsSummary {
  ticker: string;
  beneish: BeneishMScoreData | null;
  russell: RussellMembershipData | null;
  sloan: SloanAccrualsData | null;
  piotroski: PiotroskiScoreData | null;
}

export async function fetchQuantSignalsSummary(
  ticker: string,
): Promise<QuantSignalsSummary> {
  const r = await fetch(
    `${BASE}/quant-signals/summary?ticker=${encodeURIComponent(ticker)}`,
  );
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text) as { detail?: string };
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<QuantSignalsSummary>;
}

// ---------------------------------------------------------------------------
// EPS Revision Momentum — analyst estimate revisions (Phase F.6)
// ---------------------------------------------------------------------------

export type RevisionDirection =
  | "strong_upgrade"
  | "upgrade"
  | "neutral"
  | "downgrade"
  | "strong_downgrade";

export interface EpsRevisionPeriodData {
  period: string;
  period_label: string;
  up_7d: number;
  down_7d: number;
  up_30d: number;
  down_30d: number;
  revision_score_7d: number;
  revision_score_30d: number;
  direction: RevisionDirection;
}

export interface AnalystTargetsData {
  current_price: number | null;
  target_mean: number | null;
  target_median: number | null;
  target_high: number | null;
  target_low: number | null;
  upside_pct: number | null;
}

export interface EpsRevisionMomentumData {
  ticker: string;
  periods: EpsRevisionPeriodData[];
  targets: AnalystTargetsData | null;
  overall_direction: RevisionDirection;
  as_of_date: string;
}

export async function fetchEpsRevisionSummary(
  ticker: string,
): Promise<EpsRevisionMomentumData> {
  const r = await fetch(
    `${BASE}/eps-revision/summary?ticker=${encodeURIComponent(ticker)}`,
  );
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text) as { detail?: string };
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<EpsRevisionMomentumData>;
}

// ---------------------------------------------------------------------------
// Token Unlock Calendar — crypto vesting unlock events (Phase F.7)
// ---------------------------------------------------------------------------

export type UnlockSignal =
  | "high_risk"
  | "moderate_risk"
  | "low_risk"
  | "post_unlock_rebound";

export interface TokenUnlockEventData {
  protocol: string;
  symbol: string;
  unlock_date: string;
  days_until_unlock: number;
  unlock_tokens: number;
  unlock_usd: number | null;
  unlock_pct_circulating: number;
  category: string;
  sell_pressure_score: number;
  signal: UnlockSignal;
}

export interface TokenUnlockCalendarData {
  as_of_date: string;
  events: TokenUnlockEventData[];
  total_events: number;
  high_risk_count: number;
}

export async function fetchTokenUnlocks(
  days = 30,
): Promise<TokenUnlockCalendarData> {
  const r = await fetch(`${BASE}/token-unlocks/upcoming?days=${days}`);
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text) as { detail?: string };
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<TokenUnlockCalendarData>;
}

// ---------------------------------------------------------------------------
// DCF + Monte Carlo Valuation (Phase F.8)
// ---------------------------------------------------------------------------

export interface WACCData {
  cost_of_equity: number;
  cost_of_debt: number;
  tax_rate: number;
  debt_weight: number;
  equity_weight: number;
  wacc: number;
  beta: number;
  risk_free_rate: number;
}

export type ValuationLabel =
  | "deep_value"
  | "undervalued"
  | "fair"
  | "overvalued"
  | "overheated";

export interface DCFResultData {
  ticker: string;
  current_price: number;
  fair_value_p5: number;
  fair_value_p50: number;
  fair_value_p95: number;
  wacc_components: WACCData;
  base_fcf: number;
  npv_fcf: number;
  terminal_value_pv: number;
  margin_of_safety: number;
  valuation: ValuationLabel;
  projected_fcfs: number[];
  as_of_date: string;
}

export async function fetchDCFValuation(ticker: string): Promise<DCFResultData> {
  const r = await fetch(
    `${BASE}/dcf/valuation?ticker=${encodeURIComponent(ticker)}`,
  );
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text) as { detail?: string };
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<DCFResultData>;
}

export async function fetchWACC(ticker: string): Promise<WACCData> {
  const r = await fetch(
    `${BASE}/dcf/wacc?ticker=${encodeURIComponent(ticker)}`,
  );
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text) as { detail?: string };
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<WACCData>;
}

// ---------------------------------------------------------------------------
// Short Interest + Squeeze Risk (Phase F.9)
// ---------------------------------------------------------------------------

export type SqueezeSignal =
  | "squeeze_setup"
  | "high_short"
  | "moderate"
  | "low_short";

export interface ShortInterestData {
  ticker: string;
  short_pct_float: number | null;
  short_ratio: number | null;
  shares_short: number | null;
  shares_short_prior_month: number | null;
  short_change_pct: number | null;
  float_shares: number | null;
  avg_daily_volume: number | null;
  price_vs_52w_high: number | null;
  squeeze_risk_score: number;
  signal: SqueezeSignal;
  as_of_date: string;
}

export async function fetchShortInterestSummary(
  ticker: string,
): Promise<ShortInterestData> {
  const r = await fetch(
    `${BASE}/short-interest/summary?ticker=${encodeURIComponent(ticker)}`,
  );
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text) as { detail?: string };
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<ShortInterestData>;
}

export async function fetchSqueezeScan(
  tickers: string[],
): Promise<ShortInterestData[]> {
  const param = tickers.map(encodeURIComponent).join(",");
  const r = await fetch(
    `${BASE}/short-interest/squeeze-scan?tickers=${param}`,
  );
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text) as { detail?: string };
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<ShortInterestData[]>;
}

// ---------------------------------------------------------------------------
// Crypto Whale / CEX Inflow Monitor (Phase F.10)
// ---------------------------------------------------------------------------

export type WhaleSignal =
  | "heavy_inflow"
  | "elevated_inflow"
  | "neutral"
  | "accumulation"
  | "heavy_accumulation";

export interface WhaleTransferData {
  tx_hash: string;
  from_address: string;
  to_address: string;
  value_eth: number;
  value_usd: number | null;
  timestamp: string;
  exchange_name: string;
  direction: "inflow" | "outflow";
}

export interface CEXInflowData {
  symbol: string;
  hours: number;
  min_eth: number;
  inflow_eth: number;
  outflow_eth: number;
  net_flow_eth: number;
  inflow_usd: number | null;
  pressure_score: number;
  signal: WhaleSignal;
  transfer_count: number;
  recent_transfers: WhaleTransferData[];
  as_of_date: string;
  api_key_missing: boolean;
}

export async function fetchCEXInflow(
  hours = 24,
  minEth = 100,
): Promise<CEXInflowData> {
  const r = await fetch(
    `${BASE}/crypto-whale/eth-inflow?hours=${hours}&min_eth=${minEth}`,
  );
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text) as { detail?: string };
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<CEXInflowData>;
}

export async function fetchWhaleTransfers(
  hours = 24,
  minEth = 100,
  limit = 20,
): Promise<WhaleTransferData[]> {
  const r = await fetch(
    `${BASE}/crypto-whale/recent-transfers?hours=${hours}&min_eth=${minEth}&limit=${limit}`,
  );
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text) as { detail?: string };
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<WhaleTransferData[]>;
}

// ---------------------------------------------------------------------------
// PEAD — Post-Earnings Announcement Drift (Phase F.13)
// ---------------------------------------------------------------------------

export type EarningsSurpriseGrade =
  | "large_beat"
  | "beat"
  | "inline"
  | "miss"
  | "large_miss";

export interface EarningsEventData {
  earnings_date: string;
  actual_eps: number;
  estimated_eps: number;
  surprise_pct: number;
  grade: EarningsSurpriseGrade;
}

export interface PEADSignalData {
  ticker: string;
  last_earnings: EarningsEventData;
  expected_drift_30d: number;
  expected_drift_60d: number;
  expected_drift_90d: number;
  next_earnings_date: string | null;
  interpretation: string;
  as_of_date: string;
}

export async function fetchPEADSignal(
  ticker: string,
): Promise<PEADSignalData> {
  const r = await fetch(
    `${BASE}/pead/?ticker=${encodeURIComponent(ticker)}`,
  );
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text) as { detail?: string };
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<PEADSignalData>;
}

// ---------------------------------------------------------------------------
// Price Momentum Signal — Jegadeesh-Titman (Phase F.14)
// ---------------------------------------------------------------------------

export type MomentumGrade =
  | "strong_momentum"
  | "momentum"
  | "neutral"
  | "reversal_risk"
  | "strong_reversal";

export interface MomentumSignalData {
  ticker: string;
  momentum_12_1: number;     // 12-1 month return %
  return_1m: number;
  return_3m: number;
  return_6m: number;
  high_52w: number;
  low_52w: number;
  current_price: number;
  proximity_52w_high: number; // 0-1, 1 = at 52w high
  grade: MomentumGrade;
  interpretation: string;
  as_of_date: string;
}

export async function fetchMomentumSignal(
  ticker: string,
): Promise<MomentumSignalData> {
  const r = await fetch(
    `${BASE}/momentum/?ticker=${encodeURIComponent(ticker)}`,
  );
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text) as { detail?: string };
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<MomentumSignalData>;
}

// ---------------------------------------------------------------------------
// F.15 Social Sentiment & Pump Risk (StockTwits)
// ---------------------------------------------------------------------------

export type SentimentGrade =
  | "very_bullish"
  | "bullish"
  | "neutral"
  | "bearish"
  | "very_bearish";

export type PumpRiskLevel = "high" | "elevated" | "low";

export interface SocialSentimentData {
  ticker: string;
  total_messages: number;
  bullish_count: number;
  bearish_count: number;
  bullish_ratio: number;      // 0-1
  sentiment_grade: SentimentGrade;
  pump_risk_level: PumpRiskLevel;
  pump_risk_score: number;    // 0-1
  interpretation: string;
  as_of_date: string;
  api_accessible: boolean;
}

export async function fetchSocialSentiment(
  ticker: string,
): Promise<SocialSentimentData> {
  // Always returns 200; api_accessible=false when StockTwits is unreachable
  const r = await fetch(
    `${BASE}/social-sentiment/?ticker=${encodeURIComponent(ticker)}`,
  );
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text) as { detail?: string };
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<SocialSentimentData>;
}

// ---------------------------------------------------------------------------
// F.16 Unusual Options Activity (UOA)
// ---------------------------------------------------------------------------

export type OptionsGrade =
  | "bullish_unusual"
  | "bearish_unusual"
  | "mixed_unusual"
  | "neutral";

export interface UnusualContractData {
  ticker: string;
  expiry: string;
  strike: number;
  option_type: "call" | "put";
  volume: number;
  open_interest: number;
  volume_oi_ratio: number;
  implied_volatility: number;
  in_the_money: boolean;
  is_unusual: boolean;
}

export interface UnusualOptionsData {
  ticker: string;
  total_unusual_calls: number;
  total_unusual_puts: number;
  total_call_volume: number;
  total_put_volume: number;
  put_call_ratio: number;
  grade: OptionsGrade;
  top_unusual: UnusualContractData[];
  interpretation: string;
  as_of_date: string;
  data_available: boolean;
}

export async function fetchUnusualOptions(
  ticker: string,
): Promise<UnusualOptionsData> {
  // Always returns 200; data_available=false when yfinance options unavailable
  const r = await fetch(
    `${BASE}/unusual-options/?ticker=${encodeURIComponent(ticker)}`,
  );
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text) as { detail?: string };
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<UnusualOptionsData>;
}

// ---------------------------------------------------------------------------
// F.17 Pre-Earnings Expected Move
// ---------------------------------------------------------------------------

export type EarningsMoveGrade =
  | "large_expected"
  | "medium_expected"
  | "small_expected"
  | "no_data";

export interface EarningsMoveData {
  ticker: string;
  next_earnings_date: string | null;
  days_to_earnings: number | null;
  expected_move_pct: number | null;   // ±% options-implied move
  atm_strike: number | null;
  straddle_price: number | null;
  current_price: number | null;
  grade: EarningsMoveGrade;
  interpretation: string;
  as_of_date: string;
  data_available: boolean;
}

export async function fetchEarningsMove(
  ticker: string,
): Promise<EarningsMoveData> {
  const r = await fetch(
    `${BASE}/earnings-move/?ticker=${encodeURIComponent(ticker)}`,
  );
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text) as { detail?: string };
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<EarningsMoveData>;
}

// ---------------------------------------------------------------------------
// F.18 Sector Momentum Heatmap
// ---------------------------------------------------------------------------

export interface SectorReturnData {
  ticker: string;
  sector_name: string;
  return_1m: number;
  return_3m: number;
  return_6m: number;
  vs_spy_1m: number;
  grade: "leading" | "in_line" | "lagging";
}

export interface SectorMomentumData {
  sectors: SectorReturnData[];
  top3: SectorReturnData[];
  bottom3: SectorReturnData[];
  spy_return_1m: number;
  as_of_date: string;
  data_available: boolean;
}

export async function fetchSectorMomentum(): Promise<SectorMomentumData> {
  const r = await fetch(`${BASE}/sector-momentum/`);
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text) as { detail?: string };
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<SectorMomentumData>;
}

// ---------------------------------------------------------------------------
// F.19 Analyst Consensus & Price Target
// ---------------------------------------------------------------------------

export type AnalystGrade =
  | "strong_buy"
  | "buy"
  | "hold"
  | "sell"
  | "strong_sell"
  | "no_coverage";

export interface AnalystConsensusData {
  ticker: string;
  recommendation_mean: number | null;   // 1-5 (1=Strong Buy)
  recommendation_key: string | null;
  num_analysts: number;
  target_mean_price: number | null;
  target_high_price: number | null;
  target_low_price: number | null;
  current_price: number | null;
  upside_pct: number | null;            // (target_mean / current - 1) × 100
  grade: AnalystGrade;
  interpretation: string;
  as_of_date: string;
  data_available: boolean;
}

export async function fetchAnalystConsensus(
  ticker: string,
): Promise<AnalystConsensusData> {
  const r = await fetch(
    `${BASE}/analyst-consensus/?ticker=${encodeURIComponent(ticker)}`,
  );
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text) as { detail?: string };
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<AnalystConsensusData>;
}

// ── Earnings Quality (F.20) ───────────────────────────────────────────────────

export type EarningsQualityGrade =
  | "high_quality"
  | "average_quality"
  | "low_quality"
  | "manipulator_risk";

export type FScoreGrade = "very_strong" | "strong" | "average" | "weak";

export type AccrualQuality = "high" | "medium" | "low";

export interface EarningsQualityData {
  ticker: string;
  f_score: number | null;
  f_score_grade: FScoreGrade | null;
  f_score_components: Record<string, boolean>;
  m_score: number | null;
  manipulation_risk: boolean;
  accrual_ratio: number | null;
  accrual_quality: AccrualQuality | null;
  quality_grade: EarningsQualityGrade;
  interpretation: string;
  as_of_date: string;
  data_available: boolean;
}

export async function fetchEarningsQuality(
  ticker: string
): Promise<EarningsQualityData> {
  const r = await fetch(`${BASE}/earnings-quality/?ticker=${encodeURIComponent(ticker)}`);
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text);
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<EarningsQualityData>;
}

// ── Insider Trading Form 4 (F.21) ─────────────────────────────────────────────

export type InsiderSignal =
  | "cluster_buy"
  | "cluster_sell"
  | "mixed"
  | "neutral"
  | "no_data";

export interface InsiderTransactionItem {
  insider_name: string;
  title: string;
  transaction_date: string;
  shares: number;
  price_per_share: number | null;
  transaction_type: string; // "P" buy, "S" sell
  is_10b5_plan: boolean;
  form_url: string;
}

export interface InsiderTradingData {
  ticker: string;
  cik: string | null;
  signal: InsiderSignal;
  cluster_buy_count: number;
  cluster_sell_count: number;
  net_shares_90d: number;
  transactions: InsiderTransactionItem[];
  interpretation: string;
  as_of_date: string;
  data_available: boolean;
}

export async function fetchInsiderTrading(
  ticker: string
): Promise<InsiderTradingData> {
  const r = await fetch(`${BASE}/insider-trading/?ticker=${encodeURIComponent(ticker)}`);
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text);
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<InsiderTradingData>;
}

// ── Smart Money Flow (F.22) ───────────────────────────────────────────────────

export type SmartMoneySignal =
  | "smart_money_buy"
  | "smart_money_sell"
  | "accumulation"
  | "distribution"
  | "neutral"
  | "no_data";

export interface DailyFlowItem {
  date: string;
  large_buy_usd: number;
  large_sell_usd: number;
  buy_pressure_pct: number;
  total_large_usd: number;
  large_bar_count: number;
}

export interface SmartMoneyData {
  ticker: string;
  signal: SmartMoneySignal;
  today_buy_pressure_pct: number | null;
  avg_5d_buy_pressure_pct: number | null;
  large_threshold_usd: number | null;
  daily_flows: DailyFlowItem[];
  interpretation: string;
  as_of_date: string;
  data_available: boolean;
}

export async function fetchSmartMoney(ticker: string): Promise<SmartMoneyData> {
  const r = await fetch(`${BASE}/smart-money/?ticker=${encodeURIComponent(ticker)}`);
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text);
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<SmartMoneyData>;
}

// ── Index Rebalance (F.23) ────────────────────────────────────────────────────

export type IndexStatus = "member" | "non_member" | "unknown";

export type RebalanceRisk =
  | "high_addition_risk"
  | "moderate_addition_risk"
  | "stable"
  | "moderate_deletion_risk"
  | "high_deletion_risk"
  | "unknown";

export interface IndexMembershipItem {
  index_name: string;
  status: IndexStatus;
  market_cap_rank: number | null;
  market_cap_pct: number | null;
  rebalance_risk: RebalanceRisk;
}

export interface IndexRebalanceData {
  ticker: string;
  market_cap: number | null;
  market_cap_b: number | null;
  float_shares: number | null;
  price: number | null;
  eps_ttm: number | null;
  indices: IndexMembershipItem[];
  interpretation: string;
  as_of_date: string;
  data_available: boolean;
}

export async function fetchIndexRebalance(
  ticker: string
): Promise<IndexRebalanceData> {
  const r = await fetch(`${BASE}/index-rebalance/?ticker=${encodeURIComponent(ticker)}`);
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text);
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<IndexRebalanceData>;
}

// ── F.24 IV Rank & Volatility Monitor ──────────────────────────────────────

export type IVSignal =
  | "buy_options"
  | "sell_options"
  | "neutral"
  | "no_data";

export interface TermStructurePoint {
  expiry: string;
  days_to_expiry: number;
  atm_iv: number; // percentage, e.g. 25.5 means 25.5%
}

export interface IVRankData {
  ticker: string;
  current_iv: number | null;   // ATM IV, percentage
  iv_rank: number | null;      // 0–100
  iv_percentile: number | null; // 0–100
  hv10: number | null;
  hv20: number | null;
  hv30: number | null;
  hv60: number | null;
  put_call_skew: number | null; // pp
  term_structure: TermStructurePoint[];
  iv_signal: IVSignal;
  interpretation: string;
  as_of_date: string;
  data_available: boolean;
}

// ── F.25 Earnings Calendar & Expected Move ─────────────────────────────────

export type StraddleSignal =
  | "buy_straddle"
  | "sell_straddle"
  | "fair"
  | "unknown";

export interface EarningsMove {
  date: string;
  actual_move_pct: number;
  abs_move_pct: number;
  beat_estimate: boolean | null;
}

export interface EarningsCalendarData {
  ticker: string;
  next_earnings_date: string | null;
  days_to_earnings: number | null;
  implied_move_pct: number | null;
  historical_avg_move_pct: number | null;
  historical_moves: EarningsMove[];
  straddle_signal: StraddleSignal;
  interpretation: string;
  as_of_date: string;
  data_available: boolean;
}

export async function fetchEarningsCalendar(
  ticker: string
): Promise<EarningsCalendarData> {
  const r = await fetch(
    `${BASE}/earnings-calendar?ticker=${encodeURIComponent(ticker)}`
  );
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text);
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<EarningsCalendarData>;
}

export async function fetchIVRank(ticker: string): Promise<IVRankData> {
  const r = await fetch(`${BASE}/iv-rank?ticker=${encodeURIComponent(ticker)}`);
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text);
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<IVRankData>;
}

// ── F.26 Put/Call Ratio & Options Sentiment ────────────────────────────────

export type PCRSentiment =
  | "extreme_bearish"
  | "bearish"
  | "neutral"
  | "bullish"
  | "extreme_bullish"
  | "unknown";

export interface ExpiryPCR {
  expiry: string;
  days_to_expiry: number;
  call_volume: number;
  put_volume: number;
  volume_pcr: number | null;
  call_oi: number;
  put_oi: number;
  oi_pcr: number | null;
}

export interface PCRData {
  ticker: string;
  volume_pcr: number | null;
  oi_pcr: number | null;
  total_call_volume: number;
  total_put_volume: number;
  total_call_oi: number;
  total_put_oi: number;
  expiry_breakdown: ExpiryPCR[];
  sentiment: PCRSentiment;
  interpretation: string;
  as_of_date: string;
  data_available: boolean;
}

// ── F.27 Macro Dashboard ───────────────────────────────────────────────────

export type MacroRegime =
  | "risk_on"
  | "neutral"
  | "risk_off"
  | "extreme_risk_off"
  | "unknown";

export interface MacroDashboardData {
  vix: number | null;
  vix_pct_52w: number | null;
  yield_10y: number | null;
  yield_3m: number | null;
  yield_spread: number | null;
  yield_curve_inverted: boolean;
  dxy: number | null;
  gold: number | null;
  oil: number | null;
  regime: MacroRegime;
  interpretation: string;
  as_of_date: string;
  data_available: boolean;
}

export async function fetchMacroDashboard(): Promise<MacroDashboardData> {
  const r = await fetch(`${BASE}/macro-dashboard`);
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text);
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<MacroDashboardData>;
}

export async function fetchPutCallRatio(ticker: string): Promise<PCRData> {
  const r = await fetch(
    `${BASE}/put-call-ratio?ticker=${encodeURIComponent(ticker)}`
  );
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text);
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<PCRData>;
}

// ── Max Pain Calculator (F.29) ────────────────────────────────────────────

export type MaxPainSignal =
  | "pin_zone"
  | "bullish_pull"
  | "bearish_pull"
  | "weak_pull"
  | "unknown";

export interface ExpiryMaxPain {
  expiry: string;
  dte: number;
  max_pain_strike: number;
  current_price: number;
  distance_pct: number;
  total_call_oi: number;
  total_put_oi: number;
  signal: MaxPainSignal;
}

export interface MaxPainData {
  ticker: string;
  current_price: number | null;
  expiries: ExpiryMaxPain[];
  interpretation: string;
  as_of_date: string;
  data_available: boolean;
}

export async function fetchMaxPain(ticker: string): Promise<MaxPainData> {
  const r = await fetch(
    `${BASE}/max-pain?ticker=${encodeURIComponent(ticker)}`
  );
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text);
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<MaxPainData>;
}

// ── Dividend Analysis (F.28) ───────────────────────────────────────────────

export type DividendSafety =
  | "safe"
  | "watch"
  | "danger"
  | "no_dividend";

export type DividendCaptureSignal =
  | "capture_opportunity"
  | "not_applicable"
  | "unknown";

export interface DividendAnnualEntry {
  year: number;
  total: number;
}

export interface DividendAnalysisData {
  ticker: string;
  dividend_yield: number | null;
  annual_dividend: number | null;
  payout_ratio: number | null;
  ex_dividend_date: string | null;
  days_to_ex_date: number | null;
  dividend_frequency: number | null;
  five_yr_growth_rate: number | null;
  consecutive_growth_years: number;
  safety: DividendSafety;
  capture_signal: DividendCaptureSignal;
  interpretation: string;
  as_of_date: string;
  data_available: boolean;
  historical_annual: DividendAnnualEntry[];
}

export async function fetchDividendAnalysis(
  ticker: string
): Promise<DividendAnalysisData> {
  const r = await fetch(
    `${BASE}/dividend-analysis?ticker=${encodeURIComponent(ticker)}`
  );
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text);
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<DividendAnalysisData>;
}

// ── Technical Momentum Score (F.30) ──────────────────────────────────────

export type TechSignal =
  | "strong_buy"
  | "buy"
  | "neutral"
  | "sell"
  | "strong_sell"
  | "no_data";

export interface TechnicalScoreData {
  ticker: string;
  current_price: number | null;
  rsi14: number | null;
  macd_line: number | null;
  macd_signal: number | null;
  macd_histogram: number | null;
  bb_position: number | null;
  volume_ratio: number | null;
  week52_position: number | null;
  composite_score: number | null;
  signal: TechSignal;
  interpretation: string;
  as_of_date: string;
  data_available: boolean;
}

export async function fetchTechnicalScore(
  ticker: string
): Promise<TechnicalScoreData> {
  const r = await fetch(
    `${BASE}/technical-score?ticker=${encodeURIComponent(ticker)}`
  );
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text);
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<TechnicalScoreData>;
}

// ── Beta & Correlation Monitor (F.31) ─────────────────────────────────────

export type BetaSignal =
  | "high_beta"
  | "moderate_beta"
  | "low_beta"
  | "defensive"
  | "no_data";

export interface BetaCorrelationData {
  ticker: string;
  beta_1y: number | null;
  beta_63d: number | null;
  corr_spy_1y: number | null;
  corr_qqq_1y: number | null;
  r_squared_1y: number | null;
  idio_vol_ann: number | null;
  signal: BetaSignal;
  interpretation: string;
  as_of_date: string;
  data_available: boolean;
}

export async function fetchBetaCorrelation(
  ticker: string
): Promise<BetaCorrelationData> {
  const r = await fetch(
    `${BASE}/beta-correlation?ticker=${encodeURIComponent(ticker)}`
  );
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text);
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<BetaCorrelationData>;
}

// ── F.32 Seasonality Pattern Analysis ────────────────────────────────────────

export type SeasonalSignal =
  | "strong_season"
  | "positive_season"
  | "neutral"
  | "negative_season"
  | "strong_negative"
  | "no_data";

export interface MonthStats {
  month: number;
  month_name: string;
  avg_return: number;
  median_return: number;
  positive_rate: number;
  sample_size: number;
}

export interface SeasonalityData {
  ticker: string;
  current_month: number;
  current_month_stats: MonthStats | null;
  all_months: MonthStats[];
  best_month: number | null;
  worst_month: number | null;
  signal: SeasonalSignal;
  interpretation: string;
  as_of_date: string;
  data_available: boolean;
}

export async function fetchSeasonality(
  ticker: string
): Promise<SeasonalityData> {
  const r = await fetch(
    `${BASE}/seasonality?ticker=${encodeURIComponent(ticker)}`
  );
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text);
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<SeasonalityData>;
}

// ── F.33 Relative Strength Score ─────────────────────────────────────────────

export type RSSignal =
  | "strong_outperformer"
  | "outperformer"
  | "neutral"
  | "underperformer"
  | "strong_underperformer"
  | "no_data";

export interface PeriodRS {
  period: string;        // "1M" | "3M" | "6M" | "12M"
  stock_return: number;
  spy_return: number;
  relative_return: number;
}

export interface RSData {
  ticker: string;
  periods: PeriodRS[];
  rs_score: number;
  signal: RSSignal;
  interpretation: string;
  as_of_date: string;
  data_available: boolean;
}

export async function fetchRelativeStrength(
  ticker: string
): Promise<RSData> {
  const r = await fetch(
    `${BASE}/relative-strength?ticker=${encodeURIComponent(ticker)}`
  );
  if (!r.ok) {
    const text = await r.text();
    try {
      const j = JSON.parse(text);
      throw new Error(j.detail ?? text);
    } catch {
      throw new Error(text || `HTTP ${r.status}`);
    }
  }
  return r.json() as Promise<RSData>;
}


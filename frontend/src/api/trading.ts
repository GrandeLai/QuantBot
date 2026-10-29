/**
 * 统一交易 API 客户端.
 * Broker sandbox/testnet 为主目标，mock provider 为本地兜底。
 */

const BASE = "/api/trading";

export type TradingProviderKind = "futu" | "longbridge" | "mock";
export type TradingMode = "paper";
export type TradingMarket = "US" | "HK" | "UNKNOWN" | "CRYPTO";
export type TradingAssetType = "stock" | "etf" | "warrant" | "option" | "otc" | "unknown";
export type TradingOrderSide = "buy" | "sell";
export type TradingOrderType = "market" | "limit";
export type TradingOrderStatus =
  | "pending_submit"
  | "submitted"
  | "partial_filled"
  | "filled"
  | "canceled"
  | "rejected"
  | "expired"
  | "unknown";
export type TradingOrderEventType =
  | "submitted"
  | "partial_filled"
  | "filled"
  | "canceled"
  | "rejected"
  | "risk_rejected"
  | "updated";
export type TradingSessionStatus =
  | "regular"
  | "pre_market"
  | "post_market"
  | "closed"
  | "midday_break"
  | "unknown";

export interface TradingCapability {
  supported_markets: TradingMarket[];
  supported_asset_types: TradingAssetType[];
  supported_order_types: TradingOrderType[];
  supports_us_short_selling: boolean;
  supports_otc: boolean;
  supports_us_prepost: boolean;
  supports_options: boolean;
  notes: string[];
}

export interface TradingProviderStatus {
  provider: TradingProviderKind;
  mode: TradingMode;
  configured: boolean;
  using_mock_fallback: boolean;
  reason?: string | null;
  capabilities: TradingCapability;
}

export interface TradingSecurity {
  symbol: string;
  name: string;
  market: TradingMarket;
  currency: string;
  asset_type: TradingAssetType;
  lot_size: number;
  tradeable: boolean;
  shortable: boolean;
  restrictions: string[];
}

export interface TradingQuote {
  symbol: string;
  name: string;
  market: TradingMarket;
  currency: string;
  asset_type: TradingAssetType;
  last_price: number;
  prev_close: number;
  change: number;
  change_pct: number;
  trade_session: TradingSessionStatus;
  trade_status: string;
  tradeable: boolean;
  restrictions: string[];
}

export interface TradingCashInfo {
  currency: string;
  available_cash: number;
  withdraw_cash: number;
  frozen_cash: number;
  settling_cash: number;
}

export interface TradingAccountOverview {
  provider: TradingProviderKind;
  mode: TradingMode;
  currency: string;
  total_assets: number;
  available_cash: number;
  withdrawable_cash: number;
  buying_power: number;
  positions_market_value: number;
  today_pnl: number;
  today_pnl_pct: number;
  total_pnl: number;
  total_pnl_pct: number;
  cash_infos: TradingCashInfo[];
  updated_at: string;
  warnings: string[];
}

export interface TradingPosition {
  symbol: string;
  name: string;
  market: TradingMarket;
  currency: string;
  asset_type: TradingAssetType;
  quantity: number;
  available_quantity: number;
  cost_price: number;
  last_price: number;
  market_value: number;
  unrealized_pnl: number;
  unrealized_pnl_pct: number;
  day_pnl: number;
  day_pnl_pct: number;
}

export interface TradingOrder {
  order_id: string;
  symbol: string;
  name: string;
  market: TradingMarket;
  currency: string;
  asset_type: TradingAssetType;
  side: TradingOrderSide;
  order_type: TradingOrderType;
  status: TradingOrderStatus;
  quantity: number;
  executed_quantity: number;
  submitted_price?: number | null;
  trigger_price?: number | null;
  executed_price?: number | null;
  submitted_at: string;
  updated_at: string;
  message?: string | null;
}

export interface TradingExecution {
  execution_id: string;
  order_id: string;
  symbol: string;
  name: string;
  market: TradingMarket;
  currency: string;
  asset_type: TradingAssetType;
  side: TradingOrderSide;
  price: number;
  quantity: number;
  executed_at: string;
}

export interface TradingOrderEvent {
  event_id: string;
  order_id: string;
  event_type: TradingOrderEventType;
  status: TradingOrderStatus;
  message: string;
  occurred_at: string;
}

export interface TradingExecutionReport {
  order_id: string;
  status: TradingOrderStatus;
  submitted_quantity: number;
  executed_quantity: number;
  fill_ratio: number;
  event_count: number;
  lifecycle_seconds: number;
  execution_count: number;
  avg_execution_price?: number | null;
  first_execution_at?: string | null;
  last_execution_at?: string | null;
  execution_span_seconds: number;
  price_delta?: number | null;
  slippage_bps?: number | null;
}

export interface TradingRiskStatus {
  enabled: boolean;
  halted: boolean;
  max_position_count: number;
  max_single_position_pct: number;
  daily_loss_limit_pct?: number | null;
  max_order_value?: number | null;
  current_today_pnl_pct: number;
  open_position_count: number;
  available_position_slots: number;
  largest_position_symbol?: string | null;
  largest_position_ratio: number;
  warnings: string[];
}

export interface TradingCashFlow {
  cash_flow_id: string;
  currency: string;
  amount: number;
  balance: number;
  business_type: string;
  direction: string;
  description: string;
  symbol?: string | null;
  occurred_at: string;
}

export interface TradingOrderEstimate {
  symbol: string;
  side: TradingOrderSide;
  order_type: TradingOrderType;
  reference_price: number;
  cash_max_qty: number;
  sell_max_qty: number;
  reason?: string | null;
}

export interface TradingSubmitResult {
  provider: TradingProviderKind;
  order_id: string;
  status: TradingOrderStatus;
  message: string;
}

export interface TradingCancelResult {
  provider: TradingProviderKind;
  order_id: string;
  status: TradingOrderStatus;
  message: string;
}

export interface PagedResponse<T> {
  items: T[];
  page: number;
  page_size: number;
  total: number;
}

function unwrapError(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const maybe = detail as { message?: string; detail?: string };
    return maybe.message ?? maybe.detail ?? "请求失败";
  }
  return "请求失败";
}

async function request<T>(input: string, init?: RequestInit): Promise<T> {
  const res = await fetch(input, init);
  if (!res.ok) {
    const text = await res.text();
    let detail: unknown = text;
    try {
      const body = JSON.parse(text) as { detail?: unknown };
      if (body.detail !== undefined) detail = body.detail;
    } catch {
      // not JSON — use raw text as-is
    }
    throw new Error(unwrapError(detail));
  }
  return (await res.json()) as T;
}

export async function fetchTradingStatus(): Promise<TradingProviderStatus> {
  return request<TradingProviderStatus>(`${BASE}/status`);
}

export async function searchTradingSecurities(q: string): Promise<TradingSecurity[]> {
  const json = await request<{ items: TradingSecurity[] }>(
    `${BASE}/securities/search?q=${encodeURIComponent(q)}`,
  );
  return json.items;
}

export async function fetchTradingQuotes(symbols: string[]): Promise<TradingQuote[]> {
  const qs = symbols.map((symbol) => `symbol=${encodeURIComponent(symbol)}`).join("&");
  const json = await request<{ items: TradingQuote[] }>(`${BASE}/quotes?${qs}`);
  return json.items;
}

export async function fetchTradingAccount(): Promise<TradingAccountOverview> {
  return request<TradingAccountOverview>(`${BASE}/account`);
}

export async function fetchTradingRiskStatus(): Promise<TradingRiskStatus> {
  return request<TradingRiskStatus>(`${BASE}/risk`);
}

export async function fetchTradingPositions(): Promise<TradingPosition[]> {
  const json = await request<{ items: TradingPosition[] }>(`${BASE}/positions`);
  return json.items;
}

export async function fetchTodayOrders(page = 1, pageSize = 10): Promise<PagedResponse<TradingOrder>> {
  return request<PagedResponse<TradingOrder>>(`${BASE}/orders/today?page=${page}&page_size=${pageSize}`);
}

export async function fetchHistoryOrders(page = 1, pageSize = 10): Promise<PagedResponse<TradingOrder>> {
  return request<PagedResponse<TradingOrder>>(`${BASE}/orders/history?page=${page}&page_size=${pageSize}`);
}

export async function fetchOrderDetail(orderId: string): Promise<TradingOrder> {
  return request<TradingOrder>(`${BASE}/orders/${encodeURIComponent(orderId)}`);
}

export async function fetchOrderEvents(orderId: string): Promise<TradingOrderEvent[]> {
  const json = await request<{ items: TradingOrderEvent[] }>(`${BASE}/orders/${encodeURIComponent(orderId)}/events`);
  return json.items;
}

export async function fetchOrderReport(orderId: string): Promise<TradingExecutionReport> {
  return request<TradingExecutionReport>(`${BASE}/orders/${encodeURIComponent(orderId)}/report`);
}

export async function estimateTradingOrder(payload: {
  symbol: string;
  side: TradingOrderSide;
  order_type: TradingOrderType;
  submitted_price?: number;
}): Promise<TradingOrderEstimate> {
  return request<TradingOrderEstimate>(`${BASE}/orders/estimate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function submitTradingOrder(payload: {
  symbol: string;
  side: TradingOrderSide;
  order_type: TradingOrderType;
  quantity: number;
  submitted_price?: number;
}): Promise<TradingSubmitResult> {
  return request<TradingSubmitResult>(`${BASE}/orders`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function cancelTradingOrder(orderId: string): Promise<TradingCancelResult> {
  return request<TradingCancelResult>(`${BASE}/orders/${encodeURIComponent(orderId)}`, {
    method: "DELETE",
  });
}

export async function fetchTodayExecutions(page = 1, pageSize = 10): Promise<PagedResponse<TradingExecution>> {
  return request<PagedResponse<TradingExecution>>(`${BASE}/executions/today?page=${page}&page_size=${pageSize}`);
}

export async function fetchHistoryExecutions(page = 1, pageSize = 10): Promise<PagedResponse<TradingExecution>> {
  return request<PagedResponse<TradingExecution>>(`${BASE}/executions/history?page=${page}&page_size=${pageSize}`);
}

export async function fetchCashFlows(page = 1, pageSize = 10): Promise<PagedResponse<TradingCashFlow>> {
  return request<PagedResponse<TradingCashFlow>>(`${BASE}/cash-flows?page=${page}&page_size=${pageSize}`);
}

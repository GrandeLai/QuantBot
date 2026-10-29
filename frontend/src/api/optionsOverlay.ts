export type OverlayPosition = { symbol: string; quantity: string };
export type OverlayCurve = {
  date: string; equity: number; stock_pnl: number; option_pnl: number; dividend_income: number; fees: number;
  cash: number; buying_power: number; dividend_receivable: number;
  greeks: { complete: boolean; dollar_delta: number | null; residual_to_option_pnl: number; basis: string };
};
export type OverlayResult = {
  strategy: string; assignment_scenario: string; execution_completed: boolean; initial_equity: number;
  final_equity: number | null; return_fraction: number | null; excess_return_vs_hold: number | null;
  max_drawdown_fraction: number | null; option_trades: number; assignments: number; fees: number;
  opening_premium_received: number; opening_premium_paid: number; equity_curve: OverlayCurve[];
  errors: { date: string; error: string }[];
  decisions: { planned: boolean; reason: string; symbol: string; date?: string }[];
  events: { type: string; date?: string; at?: string; reason?: string; contract_id?: string; action?: string }[];
  ending_positions: { stocks: Record<string, number>; options: Record<string, number> };
};
export type OverlayReport = {
  execution_completed: boolean; research_conclusion: string; results: OverlayResult[]; download_url: string;
  data_quality: { data_kind: string; source: string; sessions: number; contracts: number; quote_versions: number };
  model_calls: number; broker_calls: number; evidence_boundary: string;
};

export function overlayPortfolio(cash: string, rows: OverlayPosition[]) {
  const balance = Number(cash);
  if (!cash.trim() || !Number.isFinite(balance) || balance < 0) throw new Error("现金须为非负数值");
  const positions: Record<string, number> = {};
  for (const row of rows) {
    const symbol = row.symbol.trim();
    const quantity = Number(row.quantity);
    if (!symbol || !Number.isSafeInteger(quantity) || quantity <= 0) throw new Error("每项持仓需填写标的及正整数股数");
    if (Object.prototype.hasOwnProperty.call(positions, symbol)) throw new Error(`持仓标的重复：${symbol}`);
    Object.defineProperty(positions, symbol, { value: quantity, enumerable: true });
  }
  if (!rows.length) throw new Error("至少需要一项已有持仓");
  return { cash: balance, positions };
}

export async function optionsRequest<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch("/api/quantbot/options" + path, body === undefined ? undefined : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "期权回测请求未通过验证");
  return result;
}

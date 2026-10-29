/** OKX 加密货币 API 客户端（现货 / 合约 / 期权）*/

export interface CryptoPair {
  symbol: string;   // BTCUSDT
  display: string;  // BTC/USDT
  name: string;
  price: number;
}

export interface CryptoTicker {
  symbol: string;
  display: string;
  price: number;
  change_pct: number;
  volume_usdt: number;
  high_24h: number;
  low_24h: number;
}

export interface CryptoBalance {
  asset: string;
  free: number;
  locked: number;
  total: number;
}

export interface CryptoAccountOverview {
  testnet: boolean;
  balances: CryptoBalance[];
  total_usdt_value: number;
  updated_at: string;
}

export interface CryptoOrder {
  order_id: string;
  symbol: string;
  display: string;
  side: "buy" | "sell";
  order_type: "market" | "limit";
  status: string;
  quantity: number;
  executed_quantity: number;
  submitted_price: number | null;
  executed_price: number | null;
  submitted_at: string;
  updated_at: string;
}

export interface CryptoStatus {
  provider: string;
  testnet: boolean;
  configured: boolean;
  base_url: string;
  mode: string;
  setup_hint: string;
}

export interface CryptoOrderRequest {
  symbol: string;
  side: "buy" | "sell";
  order_type: "market" | "limit";
  quantity: number;
  price?: number;
}

const BASE = "/api/crypto";

async function cryptoRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${BASE}${path}`, init);
  const text = await r.text();
  if (!r.ok) {
    let msg = text;
    try {
      const body = JSON.parse(text) as { detail?: string };
      if (body.detail) msg = body.detail;
    } catch { /* not JSON */ }
    throw new Error(msg || `HTTP ${r.status}`);
  }
  return JSON.parse(text) as T;
}

export async function getCryptoStatus(): Promise<CryptoStatus> {
  return cryptoRequest<CryptoStatus>("/status");
}

export async function getCryptoPairs(withPrice = false): Promise<CryptoPair[]> {
  const d = await cryptoRequest<{ pairs: CryptoPair[] }>(`/pairs?with_price=${withPrice}`);
  return d.pairs;
}

export async function getCryptoTicker(symbols: string[]): Promise<CryptoTicker[]> {
  const d = await cryptoRequest<{ tickers: CryptoTicker[] }>(
    `/ticker?symbols=${symbols.join(",")}`
  );
  return d.tickers;
}

export async function getCryptoPrice(symbol: string): Promise<number> {
  const d = await cryptoRequest<{ price: number }>(`/price/${encodeURIComponent(symbol)}`);
  return d.price;
}

export async function getCryptoAccount(): Promise<CryptoAccountOverview> {
  return cryptoRequest<CryptoAccountOverview>("/account");
}

export async function getCryptoOpenOrders(symbol?: string): Promise<CryptoOrder[]> {
  const qs = symbol ? `?symbol=${symbol}` : "";
  const d = await cryptoRequest<{ orders: CryptoOrder[] }>(`/orders/open${qs}`);
  return d.orders;
}

export async function getCryptoOrderHistory(symbol: string, limit = 50): Promise<CryptoOrder[]> {
  const d = await cryptoRequest<{ orders: CryptoOrder[] }>(
    `/orders/history?symbol=${symbol}&limit=${limit}`
  );
  return d.orders;
}

export async function submitCryptoOrder(req: CryptoOrderRequest): Promise<CryptoOrder> {
  return cryptoRequest<CryptoOrder>("/orders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
}

export async function cancelCryptoOrder(orderId: string, symbol: string): Promise<CryptoOrder> {
  return cryptoRequest<CryptoOrder>(
    `/orders/${orderId}?symbol=${encodeURIComponent(symbol)}`,
    { method: "DELETE" }
  );
}

// ─── 永续合约 ────────────────────────────────────────────────────────────────

export interface SwapTicker {
  inst_id: string;
  display: string;
  last: number;
  mark_px: number;
  change_pct: number;
  funding_rate: number;
  next_funding_time: string;
  open_interest: number;
  volume_usdt: number;
  high_24h: number;
  low_24h: number;
}

export interface CryptoPosition {
  pos_id: string;
  inst_id: string;
  inst_type: string;
  pos_side: string;       // long / short / net
  pos: number;
  avg_px: number;
  mark_px: number;
  upl: number;
  upl_ratio: number;
  lever: string;
  liq_px: number | null;
  margin_mode: string;
  currency: string;
  updated_at: string;
}

export interface SwapOrderRequest {
  inst_id: string;
  side: "buy" | "sell";
  order_type: "market" | "limit";
  sz: number;
  price?: number;
  pos_side: "long" | "short" | "net";
  margin_mode: "cross" | "isolated";
  lever: string;
}

export async function getFuturesTickers(): Promise<SwapTicker[]> {
  const d = await cryptoRequest<{ tickers: SwapTicker[] }>("/futures/tickers");
  return d.tickers;
}

export async function getFuturesPositions(): Promise<CryptoPosition[]> {
  const d = await cryptoRequest<{ positions: CryptoPosition[] }>("/futures/positions");
  return d.positions;
}

export async function getFuturesOpenOrders(instId?: string): Promise<CryptoOrder[]> {
  const qs = instId ? `?inst_id=${instId}` : "";
  const d = await cryptoRequest<{ orders: CryptoOrder[] }>(`/futures/orders/open${qs}`);
  return d.orders;
}

export async function submitFuturesOrder(req: SwapOrderRequest): Promise<CryptoOrder> {
  return cryptoRequest<CryptoOrder>("/futures/orders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
}

export async function cancelFuturesOrder(orderId: string, instId: string): Promise<CryptoOrder> {
  return cryptoRequest<CryptoOrder>(
    `/futures/orders/${orderId}?inst_id=${encodeURIComponent(instId)}`,
    { method: "DELETE" }
  );
}

// ─── 期权 ────────────────────────────────────────────────────────────────────

export interface OptionTicker {
  inst_id: string;
  uly: string;
  strike_px: number;
  opt_type: string;   // C / P
  exp_time: string;   // YYYYMMDD
  last: number;
  bid_px: number;
  ask_px: number;
  mark_vol: number;   // 隐含波动率
  delta: number;
  gamma: number;
  theta: number;
  vega: number;
  open_interest: number;
}

export async function getOptionsUnderlyings(): Promise<string[]> {
  const d = await cryptoRequest<{ underlyings: string[] }>("/options/underlyings");
  return d.underlyings;
}

export async function getOptionsExpiries(uly: string): Promise<string[]> {
  const d = await cryptoRequest<{ expiries: string[] }>(
    `/options/expiries?uly=${encodeURIComponent(uly)}`
  );
  return d.expiries;
}

export async function getOptionsChain(uly: string, expTime?: string): Promise<OptionTicker[]> {
  const qs = expTime ? `&exp_time=${expTime}` : "";
  const d = await cryptoRequest<{ chain: OptionTicker[] }>(
    `/options/chain?uly=${encodeURIComponent(uly)}${qs}`
  );
  return d.chain;
}

export async function getOptionsPositions(): Promise<CryptoPosition[]> {
  const d = await cryptoRequest<{ positions: CryptoPosition[] }>("/options/positions");
  return d.positions;
}

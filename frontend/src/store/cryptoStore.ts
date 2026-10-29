/**
 * OKX 加密货币交易状态管理.
 * 管理现货、永续合约、期权的行情、账户余额、持仓、订单等状态。
 */
import { create } from "zustand";
import type {
  CryptoBalance,
  CryptoOrder,
  CryptoPair,
  CryptoPosition,
  CryptoTicker,
  OptionTicker,
  SwapTicker,
} from "../api/crypto";

export type CryptoOrderSide = "buy" | "sell";
export type CryptoOrderType = "market" | "limit";
export type CryptoActiveTab = "market" | "trade" | "portfolio" | "orders" | "futures" | "options" | "chart" | "backtest";
export type FuturesPosSide = "long" | "short" | "net";
export type FuturesMarginMode = "cross" | "isolated";

interface CryptoState {
  // ── 通用 ──────────────────────────────────────────────────────────────────
  configured: boolean;
  testnet: boolean;
  loading: boolean;
  error: string | null;
  activeTab: CryptoActiveTab;

  // ── 现货 ──────────────────────────────────────────────────────────────────
  pairs: CryptoPair[];
  tickers: Record<string, CryptoTicker>;
  selectedSymbol: string;
  balances: CryptoBalance[];
  openOrders: CryptoOrder[];
  orderSide: CryptoOrderSide;
  orderType: CryptoOrderType;
  orderQty: string;
  orderPrice: string;

  // ── 永续合约 ──────────────────────────────────────────────────────────────
  swapTickers: SwapTicker[];
  swapPositions: CryptoPosition[];
  swapOpenOrders: CryptoOrder[];
  selectedSwap: string;           // e.g. "BTC-USDT-SWAP"
  futuresSide: CryptoOrderSide;
  futuresPosSide: FuturesPosSide;
  futuresOrderType: CryptoOrderType;
  futuresQty: string;
  futuresPrice: string;
  futuresLever: string;
  futuresMarginMode: FuturesMarginMode;

  // ── 期权 ──────────────────────────────────────────────────────────────────
  optionUnderlying: string;       // e.g. "BTC-USD"
  optionExpiries: string[];
  optionSelectedExpiry: string;
  optionChain: OptionTicker[];
  optionPositions: CryptoPosition[];

  // ── 图表 ──────────────────────────────────────────────────────────────────
  chartTimeframe: string;

  // ── Actions ───────────────────────────────────────────────────────────────
  setConfigured: (v: boolean, testnet: boolean) => void;
  setLoading: (v: boolean) => void;
  setError: (e: string | null) => void;
  setActiveTab: (tab: CryptoActiveTab) => void;

  // 现货
  setPairs: (pairs: CryptoPair[]) => void;
  setTickers: (tickers: CryptoTicker[]) => void;
  setSelectedSymbol: (sym: string) => void;
  setBalances: (b: CryptoBalance[]) => void;
  setOpenOrders: (orders: CryptoOrder[]) => void;
  setOrderSide: (side: CryptoOrderSide) => void;
  setOrderType: (type: CryptoOrderType) => void;
  setOrderQty: (v: string) => void;
  setOrderPrice: (v: string) => void;

  // 合约
  setSwapTickers: (t: SwapTicker[]) => void;
  setSwapPositions: (p: CryptoPosition[]) => void;
  setSwapOpenOrders: (o: CryptoOrder[]) => void;
  setSelectedSwap: (id: string) => void;
  setFuturesSide: (s: CryptoOrderSide) => void;
  setFuturesPosSide: (s: FuturesPosSide) => void;
  setFuturesOrderType: (t: CryptoOrderType) => void;
  setFuturesQty: (v: string) => void;
  setFuturesPrice: (v: string) => void;
  setFuturesLever: (v: string) => void;
  setFuturesMarginMode: (m: FuturesMarginMode) => void;

  // 期权
  setOptionUnderlying: (uly: string) => void;
  setOptionExpiries: (e: string[]) => void;
  setOptionSelectedExpiry: (e: string) => void;
  setOptionChain: (chain: OptionTicker[]) => void;
  setOptionPositions: (p: CryptoPosition[]) => void;

  // 图表
  setChartTimeframe: (tf: string) => void;
}

export const useCryptoStore = create<CryptoState>((set) => ({
  // ── 通用 ──────────────────────────────────────────────────────────────────
  configured: false,
  testnet: true,
  loading: false,
  error: null,
  activeTab: "market",

  // ── 现货 ──────────────────────────────────────────────────────────────────
  pairs: [],
  tickers: {},
  selectedSymbol: "BTC-USDT",
  balances: [],
  openOrders: [],
  orderSide: "buy",
  orderType: "market",
  orderQty: "",
  orderPrice: "",

  // ── 永续合约 ──────────────────────────────────────────────────────────────
  swapTickers: [],
  swapPositions: [],
  swapOpenOrders: [],
  selectedSwap: "BTC-USDT-SWAP",
  futuresSide: "buy",
  futuresPosSide: "long",
  futuresOrderType: "market",
  futuresQty: "",
  futuresPrice: "",
  futuresLever: "10",
  futuresMarginMode: "cross",

  // ── 期权 ──────────────────────────────────────────────────────────────────
  optionUnderlying: "BTC-USD",
  optionExpiries: [],
  optionSelectedExpiry: "",
  optionChain: [],
  optionPositions: [],

  // ── 图表 ──────────────────────────────────────────────────────────────────
  chartTimeframe: "1d",

  // ── Actions ───────────────────────────────────────────────────────────────
  setConfigured: (v, testnet) => set({ configured: v, testnet }),
  setLoading: (loading) => set({ loading }),
  setError: (error) => set({ error }),
  setActiveTab: (activeTab) => set({ activeTab }),

  setPairs: (pairs) => set({ pairs }),
  setTickers: (tickers) =>
    set({ tickers: Object.fromEntries(tickers.map((t) => [t.symbol, t])) }),
  setSelectedSymbol: (selectedSymbol) => set({ selectedSymbol }),
  setBalances: (balances) => set({ balances }),
  setOpenOrders: (openOrders) => set({ openOrders }),
  setOrderSide: (orderSide) => set({ orderSide }),
  setOrderType: (orderType) => set({ orderType }),
  setOrderQty: (orderQty) => set({ orderQty }),
  setOrderPrice: (orderPrice) => set({ orderPrice }),

  setSwapTickers: (swapTickers) => set({ swapTickers }),
  setSwapPositions: (swapPositions) => set({ swapPositions }),
  setSwapOpenOrders: (swapOpenOrders) => set({ swapOpenOrders }),
  setSelectedSwap: (selectedSwap) => set({ selectedSwap }),
  setFuturesSide: (futuresSide) => set({ futuresSide }),
  setFuturesPosSide: (futuresPosSide) => set({ futuresPosSide }),
  setFuturesOrderType: (futuresOrderType) => set({ futuresOrderType }),
  setFuturesQty: (futuresQty) => set({ futuresQty }),
  setFuturesPrice: (futuresPrice) => set({ futuresPrice }),
  setFuturesLever: (futuresLever) => set({ futuresLever }),
  setFuturesMarginMode: (futuresMarginMode) => set({ futuresMarginMode }),

  setOptionUnderlying: (optionUnderlying) => set({ optionUnderlying }),
  setOptionExpiries: (optionExpiries) => set({ optionExpiries }),
  setOptionSelectedExpiry: (optionSelectedExpiry) => set({ optionSelectedExpiry }),
  setOptionChain: (optionChain) => set({ optionChain }),
  setOptionPositions: (optionPositions) => set({ optionPositions }),

  setChartTimeframe: (chartTimeframe) => set({ chartTimeframe }),
}));

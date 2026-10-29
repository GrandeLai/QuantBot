/**
 * 交易执行状态管理.
 * 聚焦于交易票据（ticket）与当前选中标的，便于交易页和图表快捷卖出共享。
 */
import { create } from "zustand";
import type { TradingOrderSide, TradingOrderType, TradingQuote, TradingSecurity } from "../api/trading";

interface TradingDraft {
  side: TradingOrderSide;
  orderType: TradingOrderType;
  quantity: string;
  submittedPrice: string;
}

interface TradingState {
  query: string;
  selectedSecurity: TradingSecurity | null;
  quote: TradingQuote | null;
  draft: TradingDraft;
  setQuery: (query: string) => void;
  setSelectedSecurity: (security: TradingSecurity | null) => void;
  setQuote: (quote: TradingQuote | null) => void;
  patchDraft: (patch: Partial<TradingDraft>) => void;
  quickSell: (payload: { security: TradingSecurity; quantity: number }) => void;
  resetDraft: () => void;
}

const DEFAULT_DRAFT: TradingDraft = {
  side: "buy",
  orderType: "market",
  quantity: "100",
  submittedPrice: "",
};

export const useTradingStore = create<TradingState>((set) => ({
  query: "",
  selectedSecurity: null,
  quote: null,
  draft: DEFAULT_DRAFT,
  setQuery: (query) => set({ query }),
  setSelectedSecurity: (selectedSecurity) => set({ selectedSecurity }),
  setQuote: (quote) => set({ quote }),
  patchDraft: (patch) => set((state) => ({ draft: { ...state.draft, ...patch } })),
  quickSell: ({ security, quantity }) =>
    set({
      selectedSecurity: security,
      query: security.symbol,
      draft: {
        side: "sell",
        orderType: "market",
        quantity: String(quantity),
        submittedPrice: "",
      },
    }),
  resetDraft: () => set({ draft: DEFAULT_DRAFT }),
}));

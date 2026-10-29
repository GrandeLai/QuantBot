/**
 * 看盘面板状态管理 — Zustand store.
 */
import { create } from "zustand";
import type { OhlcBar, IndicatorSeries } from "../api/client";

export const TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d", "1w"] as const;
export type Timeframe = (typeof TIMEFRAMES)[number];

export type OverlayKey = "ema" | "bbands";

interface ChartState {
  symbol: string;
  timeframe: Timeframe;
  bars: OhlcBar[];
  /** key = indicator name, value = IndicatorSeries (field → aligned array) */
  indicatorData: Record<string, IndicatorSeries>;
  activeOverlays: Set<OverlayKey>;
  showVolume: boolean;
  loading: boolean;
  error: string | null;

  setSymbol: (s: string) => void;
  setTimeframe: (t: Timeframe) => void;
  setBars: (bars: OhlcBar[]) => void;
  setIndicatorData: (d: Record<string, IndicatorSeries>) => void;
  toggleOverlay: (key: OverlayKey) => void;
  toggleVolume: () => void;
  setLoading: (v: boolean) => void;
  setError: (e: string | null) => void;
}

export const useChartStore = create<ChartState>((set, get) => ({
  symbol: "AAPL",
  timeframe: "1d",
  bars: [],
  indicatorData: {},
  activeOverlays: new Set<OverlayKey>(["ema"]),
  showVolume: true,
  loading: false,
  error: null,

  setSymbol: (symbol) => set({ symbol }),
  setTimeframe: (timeframe) => set({ timeframe }),
  setBars: (bars) => set({ bars }),
  setIndicatorData: (indicatorData) => set({ indicatorData }),
  toggleOverlay: (key) => {
    const next = new Set(get().activeOverlays);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    set({ activeOverlays: next });
  },
  toggleVolume: () => set((s) => ({ showVolume: !s.showVolume })),
  setLoading: (loading) => set({ loading }),
  setError: (error) => set({ error }),
}));

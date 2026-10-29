/**
 * Screener state management — Zustand store.
 */
import { create } from "zustand";

export interface StrategyMeta {
  id: string;
  name_zh: string;
  description: string;
  min_score: number;
}

export interface ScreenResult {
  symbol: string;
  score: number;
  matched: boolean;
  trend: number;
  bias: number;
  volume: number;
  support: number;
  macd: number;
  rsi: number;
}

export interface DecisionData {
  recommendation: "BUY" | "HOLD" | "SELL";
  conviction: number;
  buy_price?: number;
  stop_loss?: number;
  summary: string;
  checklist: string[];
}

type PhaseStatus = "idle" | "running" | "done";

interface ScreenerState {
  // Strategy & config
  strategies: StrategyMeta[];
  selectedStrategyId: string;
  inputSymbols: string;
  lookbackDays: number;

  // Screening results
  screenResults: ScreenResult[];
  screenLoading: boolean;
  screenError: string | null;
  totalScreened: number;
  matchedCount: number;

  // Selected symbol for detail/analysis
  selectedSymbol: string | null;

  // 4-phase analysis state
  phaseContents: Record<1 | 2 | 3 | 4, string>;
  phaseStatus: Record<1 | 2 | 3 | 4, PhaseStatus>;
  analyzeDecision: DecisionData | null;
  analyzeLoading: boolean;

  // Actions
  setStrategies: (s: StrategyMeta[]) => void;
  setSelectedStrategy: (id: string) => void;
  setInputSymbols: (v: string) => void;
  setLookbackDays: (v: number) => void;
  setScreenResults: (r: ScreenResult[], total: number, matched: number) => void;
  setScreenLoading: (v: boolean) => void;
  setScreenError: (e: string | null) => void;
  setSelectedSymbol: (s: string | null) => void;
  appendPhaseToken: (phase: 1 | 2 | 3 | 4, token: string) => void;
  setPhaseStatus: (phase: 1 | 2 | 3 | 4, status: PhaseStatus) => void;
  setAnalyzeDecision: (d: DecisionData) => void;
  setAnalyzeLoading: (v: boolean) => void;
  resetAnalysis: () => void;
}

const EMPTY_PHASE_CONTENTS = { 1: "", 2: "", 3: "", 4: "" } as const;
const IDLE_PHASE_STATUS = { 1: "idle", 2: "idle", 3: "idle", 4: "idle" } as const;

export const useScreenerStore = create<ScreenerState>((set) => ({
  strategies: [],
  selectedStrategyId: "bull_trend",
  inputSymbols: "",
  lookbackDays: 120,
  screenResults: [],
  screenLoading: false,
  screenError: null,
  totalScreened: 0,
  matchedCount: 0,
  selectedSymbol: null,
  phaseContents: { ...EMPTY_PHASE_CONTENTS },
  phaseStatus: { ...IDLE_PHASE_STATUS },
  analyzeDecision: null,
  analyzeLoading: false,

  setStrategies: (strategies) => set({ strategies }),
  setSelectedStrategy: (selectedStrategyId) => set({ selectedStrategyId }),
  setInputSymbols: (inputSymbols) => set({ inputSymbols }),
  setLookbackDays: (lookbackDays) => set({ lookbackDays }),
  setScreenResults: (screenResults, totalScreened, matchedCount) =>
    set({ screenResults, totalScreened, matchedCount }),
  setScreenLoading: (screenLoading) => set({ screenLoading }),
  setScreenError: (screenError) => set({ screenError }),
  setSelectedSymbol: (selectedSymbol) => set({ selectedSymbol }),
  appendPhaseToken: (phase, token) =>
    set((state) => ({
      phaseContents: { ...state.phaseContents, [phase]: state.phaseContents[phase] + token },
    })),
  setPhaseStatus: (phase, status) =>
    set((state) => ({ phaseStatus: { ...state.phaseStatus, [phase]: status } })),
  setAnalyzeDecision: (analyzeDecision) => set({ analyzeDecision }),
  setAnalyzeLoading: (analyzeLoading) => set({ analyzeLoading }),
  resetAnalysis: () =>
    set({
      phaseContents: { ...EMPTY_PHASE_CONTENTS },
      phaseStatus: { ...IDLE_PHASE_STATUS },
      analyzeDecision: null,
    }),
}));

/**
 * 绘图工具状态管理.
 *
 * 架构说明：
 *   - Zustand store 存储可序列化的 UI 状态（当前工具、待确认点、测量结果等）
 *   - chartRefs 对象持有不可序列化的 lightweight-charts 实例引用
 *   - 所有绘图操作（添加趋势线、清除等）通过 chartRefs 直接操作图表 API
 */
import { create } from "zustand";
import type {
  IChartApi,
  ISeriesApi,
  UTCTimestamp,
  SeriesMarker,
  MouseEventParams,
} from "lightweight-charts";

// ── 工具类型 ───────────────────────────────────────────────────────────────────

export type DrawTool = "select" | "trendline" | "text" | "measure";

// ── 图表实例引用（模块级，不放入 Zustand）──────────────────────────────────────

interface ChartRefs {
  api: IChartApi | null;
  candle: ISeriesApi<"Candlestick"> | null;
  container: HTMLDivElement | null;
  drawn: Map<string, ISeriesApi<"Line">>;
  markers: SeriesMarker<UTCTimestamp>[];
}

export const chartRefs: ChartRefs = {
  api: null,
  candle: null,
  container: null,
  drawn: new Map(),
  markers: [],
};

/**
 * 由 CandlestickChart 调用，注册 / 注销图表 API.
 */
export function registerChartApi(
  api: IChartApi | null,
  candle: ISeriesApi<"Candlestick"> | null,
  container: HTMLDivElement | null,
) {
  chartRefs.api = api;
  chartRefs.candle = candle;
  chartRefs.container = container;
  if (!api) {
    chartRefs.drawn.clear();
    chartRefs.markers = [];
  }
}

// ── Zustand Store ──────────────────────────────────────────────────────────────

interface PendingPoint {
  time: UTCTimestamp;
  price: number;
}

interface MeasureResult {
  priceDelta: number;
  pricePct: number;
  /** Page-level pixel position for overlay */
  pageX: number;
  pageY: number;
}

interface TextDialog {
  time: UTCTimestamp;
  price: number;
  pageX: number;
  pageY: number;
}

interface DrawingState {
  activeTool: DrawTool;
  isLocked: boolean;
  isHidden: boolean;
  pendingPoint: PendingPoint | null;
  measureResult: MeasureResult | null;
  textDialog: TextDialog | null;
  /** Signals ChartMainArea to focus symbol input */
  focusSearchSignal: number;

  setActiveTool: (t: DrawTool) => void;
  setPendingPoint: (p: PendingPoint | null) => void;
  setMeasureResult: (r: MeasureResult | null) => void;
  setTextDialog: (d: TextDialog | null) => void;
  toggleLock: () => void;
  toggleHidden: () => void;
  triggerFocusSearch: () => void;
  clearAll: () => void;
}

export const useChartDrawingStore = create<DrawingState>((set) => ({
  activeTool: "select",
  isLocked: false,
  isHidden: false,
  pendingPoint: null,
  measureResult: null,
  textDialog: null,
  focusSearchSignal: 0,

  setActiveTool: (activeTool) => set({ activeTool, pendingPoint: null }),

  setPendingPoint: (pendingPoint) => set({ pendingPoint }),

  setMeasureResult: (measureResult) => set({ measureResult }),

  setTextDialog: (textDialog) => set({ textDialog }),

  toggleLock: () =>
    set((s) => {
      const next = !s.isLocked;
      chartRefs.api?.applyOptions({
        handleScroll: !next as boolean,
        handleScale: !next as boolean,
      });
      return { isLocked: next };
    }),

  toggleHidden: () =>
    set((s) => {
      const next = !s.isHidden;
      for (const series of chartRefs.drawn.values()) {
        series.applyOptions({ visible: !next });
      }
      return { isHidden: next };
    }),

  triggerFocusSearch: () =>
    set((s) => ({ focusSearchSignal: s.focusSearchSignal + 1 })),

  clearAll: () => {
    const { api, drawn, candle } = chartRefs;
    if (api) {
      for (const s of drawn.values()) {
        api.removeSeries(s);
      }
      drawn.clear();
      chartRefs.markers = [];
      candle?.setMarkers([]);
    }
    set({
      pendingPoint: null,
      measureResult: null,
      textDialog: null,
      activeTool: "select",
    });
  },
}));

// ── Click handler (called from CandlestickChart) ───────────────────────────────

/**
 * lightweight-charts subscribeClick 回调，根据当前工具处理点击.
 */
export function handleDrawingClick(params: MouseEventParams) {
  const { api, candle, container, drawn } = chartRefs;
  if (!api || !candle || !params.time || !params.point) return;

  const price = candle.coordinateToPrice(params.point.y);
  if (price === null) return;

  // Page-level coordinates for overlay positioning
  const rect = container?.getBoundingClientRect();
  const pageX = (rect?.left ?? 0) + params.point.x;
  const pageY = (rect?.top ?? 0) + params.point.y;

  const store = useChartDrawingStore.getState();
  const { activeTool, pendingPoint } = store;

  if (activeTool === "select") return;

  // ── 趋势线 ──────────────────────────────────────────────────────────────────
  if (activeTool === "trendline") {
    if (!pendingPoint) {
      store.setPendingPoint({ time: params.time as UTCTimestamp, price });
    } else {
      const t1 = pendingPoint.time;
      const t2 = params.time as UTCTimestamp;
      if (t1 === t2) {
        store.setPendingPoint(null);
        return;
      }
      const [ta, pa, tb, pb] =
        t1 < t2
          ? [t1, pendingPoint.price, t2, price]
          : [t2, price, t1, pendingPoint.price];

      const lineSeries = api.addLineSeries({
        color: "#3b82f6",
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      lineSeries.setData([
        { time: ta, value: pa },
        { time: tb, value: pb },
      ]);
      const id = `tl_${Date.now()}`;
      drawn.set(id, lineSeries);
      store.setPendingPoint(null);
      store.setActiveTool("select");
    }
    return;
  }

  // ── 测量工具 ────────────────────────────────────────────────────────────────
  if (activeTool === "measure") {
    if (!pendingPoint) {
      store.setPendingPoint({ time: params.time as UTCTimestamp, price });
    } else {
      const delta = price - pendingPoint.price;
      const pct = pendingPoint.price !== 0 ? (delta / pendingPoint.price) * 100 : 0;
      store.setMeasureResult({
        priceDelta: delta,
        pricePct: pct,
        pageX,
        pageY,
      });
      store.setPendingPoint(null);
      store.setActiveTool("select");
    }
    return;
  }

  // ── 文字注释 ────────────────────────────────────────────────────────────────
  if (activeTool === "text") {
    store.setTextDialog({
      time: params.time as UTCTimestamp,
      price,
      pageX,
      pageY,
    });
  }
}

/**
 * 提交文字注释到图表（由 TextDialog 回调调用）.
 */
export function commitTextAnnotation(
  time: UTCTimestamp,
  text: string,
) {
  const { candle } = chartRefs;
  if (!candle || !text.trim()) return;
  chartRefs.markers = [
    ...chartRefs.markers,
    {
      time,
      position: "aboveBar",
      color: "#f59e0b",
      shape: "arrowDown",
      text: text.trim(),
      size: 1,
    } as SeriesMarker<UTCTimestamp>,
  ];
  candle.setMarkers(chartRefs.markers);
}

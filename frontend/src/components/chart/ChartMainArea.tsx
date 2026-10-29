/**
 * 主图区域 — 融合 TradeFlow Pro 视觉风格与现有 CandlestickChart.
 * 顶部工具栏：标的搜索 + 拉取 + EMA 信息 + 图标
 * 主体：CandlestickChart（动态高度）
 * 底部栏：周期切换 + 指标开关
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Settings, Maximize2, RefreshCw } from "lucide-react";
import FeatureGuideButton from "@/components/guides/FeatureGuideButton";
import CandlestickChart from "../CandlestickChart";
import { useChartStore, TIMEFRAMES, type Timeframe } from "../../store/chartStore";
import { useChartDrawingStore } from "../../store/chartDrawing";
import { fetchBars, fetchBatchIndicators, triggerFetch } from "../../api/client";
import { cn } from "../../lib/utils";

// 偏移量 = App头部(56) + App底栏(32) + main顶部padding(16) + MarketPanel子tab栏(46) + ChartLayout底部面板(256) + 状态栏(24) + 图表头(40) + 图表底(32) + 余量(8)
const CHART_HEIGHT_OFFSET = 510;
const ALWAYS_FETCH = ["ema", "bbands"];

export default function ChartMainArea() {
  const {
    symbol,
    timeframe,
    bars,
    indicatorData,
    activeOverlays,
    showVolume,
    loading,
    error,
    setSymbol,
    setTimeframe,
    setBars,
    setIndicatorData,
    toggleOverlay,
    toggleVolume,
    setLoading,
    setError,
  } = useChartStore();

  const [inputSymbol, setInputSymbol] = useState(symbol);
  const [fetching, setFetching] = useState(false);
  const [fetchMsg, setFetchMsg] = useState<string | null>(null);
  const symbolInputRef = useRef<HTMLInputElement>(null);

  // 响应侧边栏"搜索"按钮的 focusSearch 信号
  const focusSearchSignal = useChartDrawingStore((s) => s.focusSearchSignal);
  useEffect(() => {
    if (focusSearchSignal > 0) symbolInputRef.current?.focus();
  }, [focusSearchSignal]);
  const [chartH, setChartH] = useState(
    () => Math.max(280, window.innerHeight - CHART_HEIGHT_OFFSET),
  );

  // 动态高度：跟随窗口 resize
  useEffect(() => {
    const update = () =>
      setChartH(Math.max(280, window.innerHeight - CHART_HEIGHT_OFFSET));
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, []);

  // 当 chartStore.symbol 被外部（关注列表）改变时，同步输入框
  useEffect(() => {
    setInputSymbol(symbol);
  }, [symbol]);

  const loadData = useCallback(
    async (sym: string, tf: Timeframe) => {
      setLoading(true);
      setError(null);
      try {
        const newBars = await fetchBars(sym, tf, 300);
        setBars(newBars);
        if (newBars.length >= 2) {
          const indData = await fetchBatchIndicators(newBars, ALWAYS_FETCH);
          setIndicatorData(indData);
        } else {
          setIndicatorData({});
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : "加载失败");
      } finally {
        setLoading(false);
      }
    },
    [setBars, setIndicatorData, setLoading, setError],
  );

  // symbol / timeframe 变化时重新加载
  useEffect(() => {
    void loadData(symbol, timeframe);
  }, [symbol, timeframe, loadData]);

  const handleSymbolSubmit = () => {
    const sym = inputSymbol.trim().toUpperCase();
    if (!sym) return;
    if (sym !== symbol) {
      setSymbol(sym);
    } else {
      void loadData(sym, timeframe);
    }
  };

  const handleFetch = async () => {
    const sym = inputSymbol.trim().toUpperCase();
    if (!sym) return;
    setFetching(true);
    setFetchMsg(null);
    try {
      const start = new Date();
      start.setFullYear(start.getFullYear() - 2);
      await triggerFetch(sym, timeframe, start.toISOString().split("T")[0]);
      setFetchMsg(`数据拉取任务已提交 (${sym})，稍后点击刷新`);
      setSymbol(sym);
    } catch (e) {
      setFetchMsg(e instanceof Error ? e.message : "拉取失败");
    } finally {
      setFetching(false);
    }
  };

  // 最新 EMA 值（用于头部显示）
  const lastEma = (() => {
    const series = indicatorData["ema"]?.["ema"];
    if (!series) return null;
    for (let i = series.length - 1; i >= 0; i--) {
      if (series[i] != null) return (series[i] as number).toFixed(2);
    }
    return null;
  })();

  const TIMEFRAME_LABELS: Record<string, string> = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1H",
    "4h": "4H",
    "1d": "D",
    "1w": "W",
  };

  return (
    <div className="flex-1 bg-[#131722] flex flex-col min-h-0 relative">
      {/* ── 顶部工具栏 ──────────────────────────────────── */}
      <div className="h-10 border-b border-gray-800 flex items-center gap-3 px-3 text-xs text-gray-400 shrink-0">
        {/* 标的搜索 */}
        <div className="flex items-center gap-1.5">
          <div className="flex items-center bg-[#2a2e39] border border-gray-700 rounded overflow-hidden">
            <input
              ref={symbolInputRef}
              value={inputSymbol}
              onChange={(e) => setInputSymbol(e.target.value.toUpperCase())}
              onKeyDown={(e) => e.key === "Enter" && handleSymbolSubmit()}
              placeholder="标的代码"
              className="bg-transparent px-2 py-1 text-white text-xs w-28 outline-none placeholder-gray-600 font-mono"
            />
          </div>
          <button
            onClick={handleSymbolSubmit}
            disabled={loading}
            className="px-2 py-1 bg-blue-600 hover:bg-blue-500 text-white rounded text-[11px] font-medium transition-colors disabled:opacity-50"
          >
            {loading ? "…" : "查询"}
          </button>
          <button
            onClick={() => void handleFetch()}
            disabled={fetching || loading}
            title="从数据源拉取最新行情"
            className="p-1 text-gray-400 hover:text-white hover:bg-[#2a2e39] rounded transition-colors disabled:opacity-40"
          >
            <RefreshCw size={13} className={fetching ? "animate-spin" : ""} />
          </button>
        </div>

        {/* 分隔线 */}
        <div className="h-4 w-px bg-gray-800" />

        {/* 标的名称 + 连接状态 */}
        <div className="flex items-center gap-2">
          <span className="text-white font-bold">{symbol}</span>
          <div className="w-1.5 h-1.5 rounded-full bg-green-500" />
        </div>

        {/* EMA 信息 */}
        {activeOverlays.has("ema") && lastEma && (
          <div className="flex items-center gap-1.5">
            <span className="text-orange-400 text-[11px]">EMA(20)</span>
            <span className="text-orange-400 font-mono text-[11px]">{lastEma}</span>
          </div>
        )}

        {/* 消息提示 */}
        {(fetchMsg || error) && (
          <span
            className={cn(
              "text-[10px] truncate max-w-48",
              error ? "text-red-400" : "text-gray-500",
            )}
          >
            {error ?? fetchMsg}
          </span>
        )}

        {/* 右侧图标 */}
        <div className="flex items-center gap-2 ml-auto">
          <Settings
            size={14}
            className="cursor-pointer hover:text-white transition-colors"
          />
          <Maximize2
            size={14}
            className="cursor-pointer hover:text-white transition-colors"
          />
        </div>
      </div>

      {/* ── K 线图主体 ────────────────────────────────── */}
      <div className="flex-1 min-h-0 bg-[#0f172a]">
        <CandlestickChart
          bars={bars}
          indicatorData={indicatorData}
          activeOverlays={activeOverlays}
          showVolume={showVolume}
          height={chartH}
        />
      </div>

      {/* ── 底部工具栏 ────────────────────────────────── */}
      <div className="h-8 border-t border-gray-800 flex items-center px-4 gap-4 text-[10px] text-gray-500 shrink-0">
        {/* 周期切换 */}
        <div className="flex gap-2">
          {TIMEFRAMES.map((tf) => (
            <button
              key={tf}
              onClick={() => setTimeframe(tf)}
              className={cn(
                "hover:text-white transition-colors",
                timeframe === tf && "text-blue-500 font-bold",
              )}
            >
              {TIMEFRAME_LABELS[tf] ?? tf}
            </button>
          ))}
        </div>

        <div className="h-3 w-px bg-gray-800" />

        {/* 指标开关 */}
        <div className="flex gap-3">
          <button
            onClick={() => toggleOverlay("ema")}
            className={cn(
              "hover:text-white transition-colors",
              activeOverlays.has("ema") && "text-orange-400",
            )}
          >
            EMA
          </button>
          <button
            onClick={() => toggleOverlay("bbands")}
            className={cn(
              "hover:text-white transition-colors",
              activeOverlays.has("bbands") && "text-purple-400",
            )}
          >
            BBands
          </button>
          <button
            onClick={() => toggleVolume()}
            className={cn(
              "hover:text-white transition-colors",
              showVolume && "text-blue-400",
            )}
          >
            成交量
          </button>
        </div>

        {/* 状态信息 */}
        {bars.length > 0 && (
          <span className="ml-auto text-gray-600">
            {bars.length} 根K线 ·{" "}
            {new Date(bars[bars.length - 1].timestamp).toLocaleDateString()}
          </span>
        )}
      </div>
      <FeatureGuideButton guideKey="market.chart.main" className="bottom-12 right-4" />
    </div>
  );
}

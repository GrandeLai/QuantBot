/**
 * 看盘面板 — 标的输入 + 周期切换 + 指标叠加 + K 线图.
 */
import { useEffect, useCallback, useState } from "react";
import { fetchBars, fetchBatchIndicators, triggerFetch } from "../api/client";
import { useChartStore, type Timeframe } from "../store/chartStore";
import CandlestickChart from "./CandlestickChart";
import TimeframeSelector from "./TimeframeSelector";
import IndicatorSelector from "./IndicatorSelector";

// 默认始终拉取的指标（叠加层用）
const ALWAYS_FETCH = ["ema", "bbands"];

export default function ChartPanel() {
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

  // 加载 K 线 + 计算指标
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
    if (sym && sym !== symbol) {
      setSymbol(sym);
    } else if (sym === symbol) {
      void loadData(sym, timeframe);
    }
  };

  // 触发数据拉取（后台任务）
  const handleFetch = async () => {
    const sym = inputSymbol.trim().toUpperCase();
    if (!sym) return;
    setFetching(true);
    setFetchMsg(null);
    try {
      // 默认拉近 2 年数据
      const start = new Date();
      start.setFullYear(start.getFullYear() - 2);
      const startStr = start.toISOString().split("T")[0];
      await triggerFetch(sym, timeframe, startStr);
      setFetchMsg(`数据拉取任务已提交 (${sym})，稍后点击刷新`);
      setSymbol(sym);
    } catch (e) {
      setFetchMsg(e instanceof Error ? e.message : "拉取失败");
    } finally {
      setFetching(false);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {/* 工具栏 */}
      <div
        style={{
          display: "flex",
          gap: 8,
          alignItems: "center",
          flexWrap: "wrap",
          padding: "10px 14px",
          background: "#0f172a",
          borderRadius: 8,
          border: "1px solid #1e293b",
        }}
      >
        {/* 标的输入 */}
        <div style={{ display: "flex", gap: 6 }}>
          <input
            value={inputSymbol}
            onChange={(e) => setInputSymbol(e.target.value.toUpperCase())}
            onKeyDown={(e) => e.key === "Enter" && handleSymbolSubmit()}
            placeholder="输入标的代码，如 AAPL"
            style={{
              padding: "5px 10px",
              fontSize: 13,
              background: "#1e293b",
              border: "1px solid #334155",
              borderRadius: 4,
              color: "#e2e8f0",
              width: 160,
              outline: "none",
            }}
          />
          <button
            onClick={handleSymbolSubmit}
            style={btnStyle("#3b82f6")}
            disabled={loading}
          >
            {loading ? "加载中…" : "查询"}
          </button>
          <button
            onClick={() => void handleFetch()}
            style={btnStyle("#10b981")}
            disabled={fetching || loading}
            title="从数据源拉取最新行情写入本地数据库"
          >
            {fetching ? "拉取中…" : "拉取数据"}
          </button>
        </div>

        <div style={{ width: 1, height: 20, background: "#334155" }} />

        {/* 周期切换 */}
        <TimeframeSelector
          value={timeframe}
          onChange={(tf) => setTimeframe(tf)}
        />

        <div style={{ width: 1, height: 20, background: "#334155" }} />

        {/* 指标开关 */}
        <IndicatorSelector
          activeOverlays={activeOverlays}
          showVolume={showVolume}
          onToggleOverlay={toggleOverlay}
          onToggleVolume={toggleVolume}
        />
      </div>

      {/* 消息提示 */}
      {fetchMsg && (
        <div
          style={{
            padding: "6px 12px",
            background: "#1e293b",
            borderRadius: 4,
            fontSize: 12,
            color: "#94a3b8",
          }}
        >
          {fetchMsg}
        </div>
      )}
      {error && (
        <div
          style={{
            padding: "6px 12px",
            background: "#450a0a",
            borderRadius: 4,
            fontSize: 12,
            color: "#fca5a5",
          }}
        >
          错误: {error}
        </div>
      )}

      {/* K 线图 */}
      <div
        style={{
          borderRadius: 8,
          overflow: "hidden",
          border: "1px solid #1e293b",
        }}
      >
        <CandlestickChart
          bars={bars}
          indicatorData={indicatorData}
          activeOverlays={activeOverlays}
          showVolume={showVolume}
          height={500}
        />
      </div>

      {/* 状态栏 */}
      {bars.length > 0 && (
        <div style={{ fontSize: 11, color: "#475569", paddingLeft: 4 }}>
          {symbol} · {timeframe} · {bars.length} 根K线 ·{" "}
          {new Date(bars[0].timestamp).toLocaleDateString()} —{" "}
          {new Date(bars[bars.length - 1].timestamp).toLocaleDateString()}
        </div>
      )}
    </div>
  );
}

function btnStyle(bg: string): React.CSSProperties {
  return {
    padding: "5px 12px",
    fontSize: 12,
    borderRadius: 4,
    border: "none",
    background: bg,
    color: "#fff",
    cursor: "pointer",
    fontWeight: 500,
  };
}

/**
 * SectorMomentumPanel — SPDR 板块动量轮动热力图
 *
 * 功能：
 * - 展示 11 个板块 ETF 的 1M/3M/6M 收益率 vs SPY
 * - 领涨/同步/滞涨 grade 标记
 * - Top3（领涨）/ Bottom3（滞涨）高亮
 *
 * Phase F.18 — 板块轮动因子
 * 数据：11 SPDR 板块 ETF + SPY 基准 via yfinance
 */
import { useEffect, useState } from "react";
import {
  type SectorMomentumData,
  type SectorReturnData,
  fetchSectorMomentum,
} from "../api/client";

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function gradeColor(grade: string): string {
  if (grade === "leading") return "#00C087";
  if (grade === "lagging") return "#ef4444";
  return "#64748b"; // in_line
}

function gradeLabel(grade: string): string {
  if (grade === "leading") return "领涨 ↑";
  if (grade === "lagging") return "滞涨 ↓";
  return "同步";
}

function retColor(pct: number): string {
  if (pct > 5) return "#00C087";
  if (pct > 0) return "#34d399";
  if (pct < -5) return "#ef4444";
  if (pct < 0) return "#f59e0b";
  return "#64748b";
}

function fmtPct(n: number): string {
  return n >= 0 ? `+${n.toFixed(1)}%` : `${n.toFixed(1)}%`;
}

// ---------------------------------------------------------------------------
// 子组件：单行板块
// ---------------------------------------------------------------------------

function SectorRow({ s, highlight }: { s: SectorReturnData; highlight?: "top" | "bottom" }) {
  const rowBg =
    highlight === "top"
      ? "#00C08711"
      : highlight === "bottom"
      ? "#ef444411"
      : "transparent";

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "70px 80px 65px 65px 65px 70px",
        gap: 4,
        padding: "5px 10px",
        borderBottom: "1px solid #1a1d24",
        alignItems: "center",
        background: rowBg,
      }}
    >
      <span style={{ color: "#e2e8f0", fontWeight: 700, fontSize: 11 }}>{s.ticker}</span>
      <span style={{ color: "#94a3b8", fontSize: 10 }}>{s.sector_name}</span>
      <span style={{ color: retColor(s.return_1m), fontWeight: 700, fontSize: 11, textAlign: "right" }}>
        {fmtPct(s.return_1m)}
      </span>
      <span style={{ color: retColor(s.return_3m), fontSize: 11, textAlign: "right" }}>
        {fmtPct(s.return_3m)}
      </span>
      <span style={{ color: retColor(s.return_6m), fontSize: 11, textAlign: "right" }}>
        {fmtPct(s.return_6m)}
      </span>
      <span
        style={{
          fontSize: 10,
          fontWeight: 700,
          color: gradeColor(s.grade),
          textAlign: "right",
          padding: "1px 4px",
          borderRadius: 3,
          background: gradeColor(s.grade) + "22",
        }}
      >
        {gradeLabel(s.grade)}
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export default function SectorMomentumPanel() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<SectorMomentumData | null>(null);

  async function loadData() {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchSectorMomentum();
      setResult(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  // Auto-load on mount
  useEffect(() => {
    void loadData();
  }, []);

  // Build a set of top/bottom tickers for row highlighting
  const topTickers = new Set(result?.top3.map((s) => s.ticker) ?? []);
  const bottomTickers = new Set(result?.bottom3.map((s) => s.ticker) ?? []);

  // Sort sectors by 1M return for display
  const sortedSectors = result
    ? [...result.sectors].sort((a, b) => b.return_1m - a.return_1m)
    : [];

  return (
    <div
      style={{
        background: "#0E1014",
        border: "1px solid #2a2d35",
        borderRadius: 10,
        padding: 16,
        marginBottom: 16,
        fontFamily: "ui-monospace, monospace",
      }}
    >
      {/* 标题 */}
      <div
        style={{
          fontWeight: 700,
          fontSize: 14,
          color: "#e2e8f0",
          marginBottom: 12,
          borderBottom: "1px solid #2a2d35",
          paddingBottom: 8,
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
        }}
      >
        <span>板块轮动热力图（SPDR ETF）</span>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span style={{ fontSize: 10, color: "#475569", fontWeight: 400 }}>
            来源：yfinance · 11 板块 ETF + SPY
          </span>
          <button
            onClick={loadData}
            disabled={loading}
            style={{
              background: loading ? "#1f2937" : "#151619",
              color: loading ? "#94a3b8" : "#00C087",
              border: "1px solid #2a2d35",
              borderRadius: 4,
              padding: "3px 10px",
              fontWeight: 700,
              fontSize: 11,
              cursor: loading ? "not-allowed" : "pointer",
            }}
          >
            {loading ? "刷新中..." : "↻ 刷新"}
          </button>
        </div>
      </div>

      {/* 错误 */}
      {error && (
        <div
          style={{
            background: "#ef444422",
            border: "1px solid #ef444455",
            borderRadius: 6,
            padding: "8px 12px",
            color: "#ef4444",
            fontSize: 12,
            marginBottom: 12,
          }}
        >
          {error}
        </div>
      )}

      {/* 加载中 */}
      {loading && !result && (
        <div style={{ textAlign: "center", color: "#475569", fontSize: 12, padding: "20px 0" }}>
          正在下载板块数据...
        </div>
      )}

      {/* 结果 */}
      {result && (
        <div>
          {/* 降级提示 */}
          {!result.data_available && (
            <div
              style={{
                background: "#f59e0b22",
                border: "1px solid #f59e0b55",
                borderRadius: 6,
                padding: "8px 12px",
                color: "#f59e0b",
                fontSize: 12,
                marginBottom: 12,
              }}
            >
              ⚠ 板块数据暂时不可用（yfinance 超时）。请点击刷新重试。
            </div>
          )}

          {/* SPY 基准 */}
          {result.data_available && (
            <div
              style={{
                background: "#151619",
                border: "1px solid #2a2d35",
                borderRadius: 6,
                padding: "6px 10px",
                marginBottom: 10,
                display: "flex",
                alignItems: "center",
                gap: 12,
                fontSize: 11,
              }}
            >
              <span style={{ color: "#64748b" }}>基准 SPY 1M：</span>
              <span style={{ color: retColor(result.spy_return_1m), fontWeight: 700 }}>
                {fmtPct(result.spy_return_1m)}
              </span>
              <span style={{ color: "#475569", fontSize: 10 }}>
                vs SPY 阈值：领涨 {'>'} +2%，滞涨 {'<'} -2%
              </span>
            </div>
          )}

          {/* 板块表格 */}
          {result.data_available && sortedSectors.length > 0 && (
            <div
              style={{
                background: "#151619",
                border: "1px solid #2a2d35",
                borderRadius: 8,
                overflow: "hidden",
                marginBottom: 10,
              }}
            >
              {/* Header */}
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "70px 80px 65px 65px 65px 70px",
                  gap: 4,
                  padding: "6px 10px",
                  borderBottom: "1px solid #2a2d35",
                  fontSize: 9,
                  fontWeight: 700,
                  color: "#475569",
                  textTransform: "uppercase",
                }}
              >
                <span>ETF</span>
                <span>板块</span>
                <span style={{ textAlign: "right" }}>1M</span>
                <span style={{ textAlign: "right" }}>3M</span>
                <span style={{ textAlign: "right" }}>6M</span>
                <span style={{ textAlign: "right" }}>vs SPY</span>
              </div>

              {sortedSectors.map((s) => (
                <SectorRow
                  key={s.ticker}
                  s={s}
                  highlight={
                    topTickers.has(s.ticker)
                      ? "top"
                      : bottomTickers.has(s.ticker)
                      ? "bottom"
                      : undefined
                  }
                />
              ))}
            </div>
          )}

          {/* 说明 */}
          {result.data_available && (
            <div
              style={{
                background: "#151619",
                border: "1px solid #2a2d35",
                borderRadius: 6,
                padding: "6px 10px",
                fontSize: 10,
                color: "#475569",
                lineHeight: 1.6,
              }}
            >
              <div>
                <span style={{ color: "#00C087" }}>■</span> 领涨（vs SPY {'>'} +2%）
                <span style={{ color: "#ef4444" }}>■</span> 滞涨（vs SPY {'<'} -2%）
                <span style={{ color: "#64748b" }}>■</span> 同步
              </div>
              <div>板块轮动是 alpha 的持续来源 — 配合单股信号（动量/PEAD/GEX）使用效果更佳</div>
            </div>
          )}

          <div style={{ marginTop: 8, fontSize: 10, color: "#475569" }}>
            数据日期：{result.as_of_date}　│　来源：yfinance　│　不构成投资建议
          </div>
        </div>
      )}
    </div>
  );
}

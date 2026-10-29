/**
 * TechnicalScorePanel — 技术动量评分面板
 *
 * 功能（需用户输入 ticker）：
 * - 综合评分仪表盘（-100 到 +100 水平条）
 * - RSI(14)：超买/超卖区
 * - MACD Histogram：动量方向
 * - 布林带位置（0-100%）
 * - 成交量比（当前/20日均量）
 * - 52 周位置（0-100%）
 * - 信号徽章：strong_buy / buy / neutral / sell / strong_sell
 *
 * Phase F.30 — Technical Momentum Score
 * 数据来源：yfinance 历史价格（免费）
 */

import { useState } from "react";
import {
  type TechSignal,
  type TechnicalScoreData,
  fetchTechnicalScore,
} from "../api/client";

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function signalColor(s: TechSignal): string {
  if (s === "strong_buy") return "#00C087";
  if (s === "buy") return "#4ade80";
  if (s === "neutral") return "#94a3b8";
  if (s === "sell") return "#fb923c";
  if (s === "strong_sell") return "#ef4444";
  return "#475569";
}

function signalLabel(s: TechSignal): string {
  const labels: Record<TechSignal, string> = {
    strong_buy: "⬆⬆ 强烈买入",
    buy: "↑ 买入",
    neutral: "→ 中性",
    sell: "↓ 卖出",
    strong_sell: "⬇⬇ 强烈卖出",
    no_data: "— 数据不足",
  };
  return labels[s];
}

function fmtNum(v: number | null, decimals = 2, suffix = ""): string {
  if (v == null) return "—";
  return `${v.toFixed(decimals)}${suffix}`;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

/** Composite score horizontal bar (-100 to +100) */
function ScoreBar({ score }: { score: number | null }) {
  if (score == null) return null;
  const clamped = Math.max(-100, Math.min(100, score));
  // Convert to 0-100% width, center = 50%
  const isPositive = clamped >= 0;
  const barWidth = Math.abs(clamped) / 2; // max 50% each side
  const color = clamped >= 60 ? "#00C087" : clamped >= 30 ? "#4ade80"
    : clamped <= -60 ? "#ef4444" : clamped <= -30 ? "#fb923c" : "#94a3b8";

  return (
    <div style={{ margin: "6px 0" }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          marginBottom: 3,
          fontSize: 10,
        }}
      >
        <span style={{ color: "#ef4444" }}>-100 强卖</span>
        <span style={{ fontSize: 14, fontWeight: 700, color }}>
          {score >= 0 ? "+" : ""}
          {score.toFixed(0)}
        </span>
        <span style={{ color: "#00C087" }}>+100 强买</span>
      </div>
      <div
        style={{
          position: "relative",
          background: "#1a1d24",
          borderRadius: 4,
          height: 10,
          overflow: "hidden",
        }}
      >
        {/* Center line */}
        <div
          style={{
            position: "absolute",
            left: "50%",
            top: 0,
            bottom: 0,
            width: 2,
            background: "#2a2d35",
          }}
        />
        {/* Score bar */}
        <div
          style={{
            position: "absolute",
            top: 1,
            bottom: 1,
            left: isPositive ? "50%" : `${50 - barWidth}%`,
            width: `${barWidth}%`,
            background: color,
            borderRadius: 3,
            transition: "width 0.4s",
          }}
        />
      </div>
    </div>
  );
}

function MetricRow({
  label,
  value,
  sub,
  color,
  barPct,
  barColor,
}: {
  label: string;
  value: string;
  sub?: string;
  color?: string;
  barPct?: number; // 0-100 for a small progress bar
  barColor?: string;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        marginBottom: 6,
        padding: "4px 8px",
        background: "#0E1014",
        borderRadius: 5,
        border: "1px solid #2a2d35",
      }}
    >
      <div style={{ flex: 1, fontSize: 10, color: "#64748b" }}>{label}</div>
      <div>
        <span style={{ fontSize: 13, fontWeight: 700, color: color ?? "#e2e8f0" }}>
          {value}
        </span>
        {sub && (
          <span style={{ fontSize: 9, color: "#64748b", marginLeft: 4 }}>
            {sub}
          </span>
        )}
      </div>
      {barPct != null && (
        <div
          style={{
            width: 60,
            height: 6,
            background: "#1a1d24",
            borderRadius: 3,
            overflow: "hidden",
          }}
        >
          <div
            style={{
              height: "100%",
              width: `${Math.min(100, Math.max(0, barPct))}%`,
              background: barColor ?? "#94a3b8",
              borderRadius: 3,
            }}
          />
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export default function TechnicalScorePanel() {
  const [ticker, setTicker] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<TechnicalScoreData | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!ticker.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const data = await fetchTechnicalScore(ticker.trim().toUpperCase());
      setResult(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }

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
        <span>技术动量评分</span>
        <span style={{ fontSize: 10, color: "#475569", fontWeight: 400 }}>
          RSI · MACD · 布林带 · 成交量 · 52w位置
        </span>
      </div>

      {/* 输入 */}
      <form
        onSubmit={handleSubmit}
        style={{ display: "flex", gap: 8, marginBottom: 12 }}
      >
        <input
          value={ticker}
          onChange={(e) => setTicker(e.target.value.toUpperCase())}
          placeholder="Ticker（如 AAPL、MSFT、NVDA）"
          style={{
            flex: 1,
            background: "#151619",
            border: "1px solid #2a2d35",
            borderRadius: 5,
            color: "#e2e8f0",
            padding: "5px 10px",
            fontSize: 12,
            fontFamily: "ui-monospace, monospace",
          }}
        />
        <button
          type="submit"
          disabled={loading || !ticker.trim()}
          style={{
            background: loading ? "#1a1d24" : "#1a2a3a",
            border: "1px solid #2a2d35",
            borderRadius: 5,
            color: loading ? "#475569" : "#60a5fa",
            padding: "5px 14px",
            fontSize: 12,
            cursor: loading || !ticker.trim() ? "not-allowed" : "pointer",
          }}
        >
          {loading ? "计算中..." : "分析"}
        </button>
      </form>

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

      {/* 结果 */}
      {result && (
        <div>
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
              ⚠ 数据不可用（yfinance 暂时无法获取）。
            </div>
          )}

          {/* 信号徽章 */}
          <div style={{ marginBottom: 12, display: "flex", alignItems: "center", gap: 10 }}>
            <span
              style={{
                display: "inline-block",
                background: signalColor(result.signal) + "22",
                border: `1px solid ${signalColor(result.signal)}55`,
                color: signalColor(result.signal),
                borderRadius: 5,
                padding: "3px 10px",
                fontSize: 12,
                fontWeight: 700,
              }}
            >
              {signalLabel(result.signal)}
            </span>
            {result.current_price != null && (
              <span style={{ fontSize: 13, color: "#e2e8f0" }}>
                ${result.current_price.toFixed(2)}
              </span>
            )}
          </div>

          {/* 综合评分条 */}
          <div
            style={{
              background: "#151619",
              border: "1px solid #2a2d35",
              borderRadius: 8,
              padding: "10px 14px",
              marginBottom: 10,
            }}
          >
            <ScoreBar score={result.composite_score} />
          </div>

          {/* 分项指标 */}
          <div
            style={{
              background: "#151619",
              border: "1px solid #2a2d35",
              borderRadius: 8,
              padding: "10px 14px",
              marginBottom: 10,
            }}
          >
            {result.rsi14 != null && (
              <MetricRow
                label="RSI(14)"
                value={fmtNum(result.rsi14)}
                sub={
                  result.rsi14 > 70 ? "超买"
                  : result.rsi14 < 30 ? "超卖"
                  : "正常"
                }
                color={
                  result.rsi14 > 70 ? "#ef4444"
                  : result.rsi14 < 30 ? "#00C087"
                  : "#e2e8f0"
                }
                barPct={result.rsi14}
                barColor={
                  result.rsi14 > 70 ? "#ef4444"
                  : result.rsi14 < 30 ? "#00C087"
                  : "#94a3b8"
                }
              />
            )}

            {result.macd_histogram != null && (
              <MetricRow
                label="MACD Histogram"
                value={result.macd_histogram >= 0 ? `+${result.macd_histogram.toFixed(4)}` : result.macd_histogram.toFixed(4)}
                sub={result.macd_histogram > 0 ? "多头动能" : "空头动能"}
                color={result.macd_histogram > 0 ? "#00C087" : "#ef4444"}
              />
            )}

            {result.bb_position != null && (
              <MetricRow
                label="布林带位置"
                value={fmtNum(result.bb_position, 1, "%")}
                sub={
                  result.bb_position > 80 ? "触及上轨"
                  : result.bb_position < 20 ? "触及下轨"
                  : "带内"
                }
                color={
                  result.bb_position > 80 ? "#ef4444"
                  : result.bb_position < 20 ? "#00C087"
                  : "#e2e8f0"
                }
                barPct={result.bb_position}
                barColor={
                  result.bb_position > 80 ? "#ef4444"
                  : result.bb_position < 20 ? "#00C087"
                  : "#94a3b8"
                }
              />
            )}

            {result.volume_ratio != null && (
              <MetricRow
                label="成交量比"
                value={fmtNum(result.volume_ratio, 2, "×均量")}
                sub={result.volume_ratio > 2 ? "放量" : result.volume_ratio < 0.5 ? "缩量" : "正常"}
                color={result.volume_ratio > 2 ? "#f59e0b" : "#e2e8f0"}
              />
            )}

            {result.week52_position != null && (
              <MetricRow
                label="52 周位置"
                value={fmtNum(result.week52_position, 1, "%")}
                sub={
                  result.week52_position > 80 ? "接近年高"
                  : result.week52_position < 20 ? "接近年低"
                  : "区间中段"
                }
                color={
                  result.week52_position > 80 ? "#f59e0b"
                  : result.week52_position < 20 ? "#94a3b8"
                  : "#e2e8f0"
                }
                barPct={result.week52_position}
                barColor="#64748b"
              />
            )}
          </div>

          {/* 解读 */}
          <div
            style={{
              background: "#151619",
              border: "1px solid #2a2d35",
              borderRadius: 6,
              padding: "8px 12px",
              fontSize: 11,
              color: "#94a3b8",
              lineHeight: 1.6,
              marginBottom: 10,
            }}
          >
            {result.interpretation}
          </div>

          {/* 说明 */}
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
            <div>评分 = RSI(±20) + MACD(±20) + BB(±20) + 成交量(±10) + 52w(±10)，满分 ±80</div>
            <div>≥ +60 强买；+30~+60 买；-30~+30 中性；-60~-30 卖；≤ -60 强卖</div>
            <div>基于 Wilder RSI + Appel MACD + Bollinger(20,2σ)；仅供参考</div>
          </div>

          <div style={{ marginTop: 8, fontSize: 10, color: "#475569" }}>
            数据日期：{result.as_of_date}　│　来源：yfinance 1Y　│　不构成投资建议
          </div>
        </div>
      )}
    </div>
  );
}

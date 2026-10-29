/**
 * RelativeStrengthPanel — 相对强度评分面板
 *
 * 功能（需用户输入 ticker）：
 * - RS 评分仪表条（0-100，IBD-style，仿百分位）
 * - 信号徽章（strong_outperformer / outperformer / neutral / underperformer / strong_underperformer）
 * - 四个时间框架对比表格（1M / 3M / 6M / 12M）：股票收益 / SPY 收益 / 超额收益
 * - 解读文字
 *
 * Phase F.33 — Relative Strength Score
 * 数据来源：yfinance 2 年日线（免费）
 */

import { useState } from "react";
import {
  type PeriodRS,
  type RSData,
  type RSSignal,
  fetchRelativeStrength,
} from "../api/client";

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function signalColor(s: RSSignal): string {
  if (s === "strong_outperformer") return "#00C087";
  if (s === "outperformer") return "#34d399";
  if (s === "neutral") return "#94a3b8";
  if (s === "underperformer") return "#f59e0b";
  if (s === "strong_underperformer") return "#ef4444";
  return "#475569";
}

function signalLabel(s: RSSignal): string {
  const labels: Record<RSSignal, string> = {
    strong_outperformer: "🟢 强领跑（RS ≥ 80）",
    outperformer: "✅ 领跑（RS 60-79）",
    neutral: "⚪ 中性（RS 40-59）",
    underperformer: "⚠ 滞后（RS 20-39）",
    strong_underperformer: "🔴 强滞后（RS < 20）",
    no_data: "— 无数据",
  };
  return labels[s];
}

function fmtPct(v: number): string {
  const s = (v * 100).toFixed(1);
  return v >= 0 ? `+${s}%` : `${s}%`;
}

function retColor(v: number): string {
  if (v > 0.05) return "#00C087";
  if (v > 0) return "#6ee7b7";
  if (v > -0.05) return "#fca5a5";
  return "#ef4444";
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

/** RS Score gauge bar: 0-100 with threshold lines at 20/40/60/80. */
function RSScoreBar({ score, signal }: { score: number; signal: RSSignal }) {
  const color = signalColor(signal);
  const thresholds = [20, 40, 60, 80];

  return (
    <div style={{ margin: "8px 0" }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          marginBottom: 4,
          fontSize: 10,
          color: "#64748b",
        }}
      >
        <span>RS 0</span>
        <span style={{ fontSize: 16, fontWeight: 800, color }}>{score.toFixed(0)}</span>
        <span>RS 100</span>
      </div>
      <div
        style={{
          position: "relative",
          background: "#1a1d24",
          borderRadius: 4,
          height: 12,
          overflow: "hidden",
        }}
      >
        {/* Threshold markers */}
        {thresholds.map((t) => (
          <div
            key={t}
            style={{
              position: "absolute",
              left: `${t}%`,
              top: 0,
              bottom: 0,
              width: 1,
              background: "#2a2d35",
            }}
          />
        ))}
        {/* Fill bar */}
        <div
          style={{
            position: "absolute",
            top: 1,
            bottom: 1,
            left: 0,
            width: `${Math.max(0, Math.min(100, score))}%`,
            background: color,
            borderRadius: 3,
            transition: "width 0.4s",
          }}
        />
      </div>
      <div
        style={{
          display: "flex",
          justifyContent: "space-around",
          fontSize: 8,
          color: "#475569",
          marginTop: 2,
        }}
      >
        <span>滞后</span>
        <span>弱</span>
        <span>中性</span>
        <span>领跑</span>
        <span>强</span>
      </div>
    </div>
  );
}

function PeriodRow({ period }: { period: PeriodRS }) {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "60px 1fr 1fr 1fr",
        gap: 6,
        alignItems: "center",
        padding: "5px 0",
        borderBottom: "1px solid #1a1d24",
        fontSize: 12,
      }}
    >
      <span style={{ color: "#94a3b8", fontWeight: 600 }}>{period.period}</span>
      <span style={{ textAlign: "right", color: retColor(period.stock_return), fontWeight: 600 }}>
        {fmtPct(period.stock_return)}
      </span>
      <span style={{ textAlign: "right", color: "#64748b" }}>
        {fmtPct(period.spy_return)}
      </span>
      <span
        style={{
          textAlign: "right",
          color: retColor(period.relative_return),
          fontWeight: 700,
        }}
      >
        {fmtPct(period.relative_return)}
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export default function RelativeStrengthPanel() {
  const [ticker, setTicker] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<RSData | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!ticker.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const data = await fetchRelativeStrength(ticker.trim().toUpperCase());
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
        <span>相对强度评分</span>
        <span style={{ fontSize: 10, color: "#475569", fontWeight: 400 }}>
          RS Score · 多周期 vs SPY
        </span>
      </div>

      {/* 输入 */}
      <form onSubmit={handleSubmit} style={{ display: "flex", gap: 8, marginBottom: 12 }}>
        <input
          value={ticker}
          onChange={(e) => setTicker(e.target.value.toUpperCase())}
          placeholder="Ticker（如 AAPL、NVDA、TSLA）"
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
                background: "#ef444422",
                border: "1px solid #ef444455",
                borderRadius: 6,
                padding: "8px 12px",
                color: "#ef4444",
                fontSize: 12,
                marginBottom: 12,
              }}
            >
              ⚠ 数据不可用（yfinance 暂时无法获取）。
            </div>
          )}

          {/* RS Score Bar */}
          <div
            style={{
              background: "#151619",
              border: "1px solid #2a2d35",
              borderRadius: 8,
              padding: "10px 14px",
              marginBottom: 10,
            }}
          >
            <RSScoreBar score={result.rs_score} signal={result.signal} />
          </div>

          {/* 信号徽章 */}
          <div style={{ marginBottom: 12 }}>
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
          </div>

          {/* 多周期对比表 */}
          {result.periods.length > 0 && (
            <div
              style={{
                background: "#151619",
                border: "1px solid #2a2d35",
                borderRadius: 8,
                padding: "10px 14px",
                marginBottom: 10,
              }}
            >
              {/* 表头 */}
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "60px 1fr 1fr 1fr",
                  gap: 6,
                  fontSize: 9,
                  color: "#475569",
                  textTransform: "uppercase",
                  letterSpacing: 0.5,
                  paddingBottom: 6,
                  borderBottom: "1px solid #2a2d35",
                  marginBottom: 4,
                }}
              >
                <span>周期</span>
                <span style={{ textAlign: "right" }}>{result.ticker}</span>
                <span style={{ textAlign: "right" }}>SPY</span>
                <span style={{ textAlign: "right" }}>超额</span>
              </div>
              {result.periods.map((p) => (
                <PeriodRow key={p.period} period={p} />
              ))}
            </div>
          )}

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
            <div>RS Score = 仿 IBD 加权相对表现（近 1M 权重最高），-50% ~ +50% 映射 0-100。</div>
            <div>Benchmark: SPY（S&P 500 ETF）│ 数据来源：yfinance 2 年日线</div>
          </div>

          <div style={{ marginTop: 8, fontSize: 10, color: "#475569" }}>
            数据日期：{result.as_of_date}　│　不构成投资建议
          </div>
        </div>
      )}
    </div>
  );
}

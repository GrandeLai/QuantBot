/**
 * DividendAnalysisPanel — 股息分析面板
 *
 * 功能（需用户输入 ticker）：
 * - 安全评级（safe / watch / danger / no_dividend）
 * - Ex-Date 倒计时 + 股息捕获信号
 * - 股息率 + 年化股息 + 派息率
 * - 5 年 CAGR + 连续增长年数
 * - 近 5 年年度股息历史柱图
 *
 * Phase F.28 — Dividend Analysis
 * 数据来源：yfinance（tk.info + tk.dividends）
 */

import { useState } from "react";
import {
  type DividendAnalysisData,
  type DividendSafety,
  type DividendCaptureSignal,
  fetchDividendAnalysis,
} from "../api/client";

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function safetyColor(s: DividendSafety): string {
  if (s === "safe") return "#00C087";
  if (s === "watch") return "#f59e0b";
  if (s === "danger") return "#ef4444";
  return "#475569"; // no_dividend
}

function safetyLabel(s: DividendSafety): string {
  const labels: Record<DividendSafety, string> = {
    safe: "✓ 安全（Payout < 75%）",
    watch: "⚠ 观察（75% ≤ Payout < 100%）",
    danger: "🔴 危险（Payout ≥ 100%）",
    no_dividend: "— 无派息",
  };
  return labels[s];
}

function captureLabel(c: DividendCaptureSignal): string {
  if (c === "capture_opportunity") return "⚡ 股息捕获机会";
  if (c === "not_applicable") return "— 非捕获窗口";
  return "— 未知";
}

function captureColor(c: DividendCaptureSignal): string {
  if (c === "capture_opportunity") return "#f59e0b";
  return "#475569";
}

function fmtPct(v: number | null, decimals = 2): string {
  if (v == null) return "—";
  return `${(v * 100).toFixed(decimals)}%`;
}

function fmtNum(v: number | null, decimals = 2, prefix = ""): string {
  if (v == null) return "—";
  return `${prefix}${v.toFixed(decimals)}`;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function MetricCard({
  label,
  value,
  color,
  sub,
}: {
  label: string;
  value: string;
  color?: string;
  sub?: string;
}) {
  return (
    <div
      style={{
        flex: 1,
        background: "#0E1014",
        border: "1px solid #2a2d35",
        borderRadius: 5,
        padding: "7px 8px",
        textAlign: "center",
        minWidth: 80,
      }}
    >
      <div style={{ fontSize: 14, fontWeight: 700, color: color ?? "#e2e8f0" }}>
        {value}
      </div>
      {sub && (
        <div style={{ fontSize: 9, color: "#64748b", marginTop: 1 }}>{sub}</div>
      )}
      <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>{label}</div>
    </div>
  );
}

function SafetyBadge({ safety }: { safety: DividendSafety }) {
  const color = safetyColor(safety);
  return (
    <span
      style={{
        display: "inline-block",
        background: color + "22",
        border: `1px solid ${color}55`,
        color,
        borderRadius: 5,
        padding: "3px 10px",
        fontSize: 12,
        fontWeight: 700,
      }}
    >
      {safetyLabel(safety)}
    </span>
  );
}

function CaptureBadge({ signal }: { signal: DividendCaptureSignal }) {
  const color = captureColor(signal);
  return (
    <span
      style={{
        display: "inline-block",
        background: color + "22",
        border: `1px solid ${color}55`,
        color,
        borderRadius: 5,
        padding: "3px 10px",
        fontSize: 12,
        fontWeight: 700,
        marginLeft: 8,
      }}
    >
      {captureLabel(signal)}
    </span>
  );
}

function HistoricalBar({
  entries,
}: {
  entries: { year: number; total: number }[];
}) {
  if (!entries || entries.length === 0) return null;
  const max = Math.max(...entries.map((e) => e.total));
  if (max <= 0) return null;

  return (
    <div
      style={{
        background: "#151619",
        border: "1px solid #2a2d35",
        borderRadius: 8,
        padding: "10px 14px",
        marginBottom: 10,
      }}
    >
      <div style={{ fontSize: 10, color: "#64748b", marginBottom: 6 }}>
        年度股息（近 {entries.length} 年）
      </div>
      <div style={{ display: "flex", alignItems: "flex-end", gap: 6, height: 60 }}>
        {entries.map((e) => {
          const heightPct = (e.total / max) * 100;
          return (
            <div
              key={e.year}
              style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center" }}
            >
              <div style={{ fontSize: 9, color: "#94a3b8", marginBottom: 2 }}>
                ${e.total.toFixed(2)}
              </div>
              <div
                style={{
                  width: "100%",
                  height: `${heightPct}%`,
                  background: "#00C087",
                  borderRadius: "2px 2px 0 0",
                  minHeight: 3,
                }}
              />
              <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>
                {e.year}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export default function DividendAnalysisPanel() {
  const [ticker, setTicker] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<DividendAnalysisData | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!ticker.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const data = await fetchDividendAnalysis(ticker.trim().toUpperCase());
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
        <span>股息分析</span>
        <span style={{ fontSize: 10, color: "#475569", fontWeight: 400 }}>
          安全评级 · Ex-Date · 增长记录 · 捕获信号
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
          placeholder="Ticker（如 AAPL、JNJ、KO）"
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
            background: loading ? "#1a1d24" : "#1a3a2a",
            border: "1px solid #2a2d35",
            borderRadius: 5,
            color: loading ? "#475569" : "#00C087",
            padding: "5px 14px",
            fontSize: 12,
            cursor: loading || !ticker.trim() ? "not-allowed" : "pointer",
          }}
        >
          {loading ? "分析中..." : "分析"}
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

          {/* 安全评级 + 捕获信号 */}
          <div style={{ marginBottom: 12 }}>
            <SafetyBadge safety={result.safety} />
            {result.capture_signal !== "unknown" && (
              <CaptureBadge signal={result.capture_signal} />
            )}
          </div>

          {/* Ex-Date 倒计时 */}
          {result.ex_dividend_date && (
            <div
              style={{
                background:
                  result.capture_signal === "capture_opportunity"
                    ? "#f59e0b11"
                    : "#151619",
                border: `1px solid ${
                  result.capture_signal === "capture_opportunity"
                    ? "#f59e0b55"
                    : "#2a2d35"
                }`,
                borderRadius: 8,
                padding: "8px 14px",
                marginBottom: 10,
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
              }}
            >
              <span style={{ fontSize: 11, color: "#94a3b8" }}>
                Ex-Dividend Date
              </span>
              <span style={{ fontSize: 13, fontWeight: 700, color: "#e2e8f0" }}>
                {result.ex_dividend_date}
              </span>
              {result.days_to_ex_date != null && (
                <span
                  style={{
                    fontSize: 11,
                    color:
                      result.days_to_ex_date <= 5 && result.days_to_ex_date >= 0
                        ? "#f59e0b"
                        : "#64748b",
                  }}
                >
                  {result.days_to_ex_date >= 0
                    ? `${result.days_to_ex_date} 天后`
                    : `已过 ${Math.abs(result.days_to_ex_date)} 天`}
                </span>
              )}
            </div>
          )}

          {/* 关键指标卡片 */}
          {result.safety !== "no_dividend" && (
            <div
              style={{
                background: "#151619",
                border: "1px solid #2a2d35",
                borderRadius: 8,
                padding: "10px 14px",
                marginBottom: 10,
              }}
            >
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                <MetricCard
                  label="股息率"
                  value={fmtPct(result.dividend_yield)}
                  color="#00C087"
                />
                <MetricCard
                  label="年化股息"
                  value={fmtNum(result.annual_dividend, 2, "$")}
                  color="#e2e8f0"
                />
                <MetricCard
                  label="派息率"
                  value={fmtPct(result.payout_ratio)}
                  color={safetyColor(result.safety)}
                />
                {result.five_yr_growth_rate != null && (
                  <MetricCard
                    label="5 年 CAGR"
                    value={fmtPct(result.five_yr_growth_rate)}
                    color={
                      result.five_yr_growth_rate > 0 ? "#00C087" : "#ef4444"
                    }
                  />
                )}
                <MetricCard
                  label="连续增长"
                  value={`${result.consecutive_growth_years} 年`}
                  color={
                    result.consecutive_growth_years >= 25
                      ? "#f59e0b"
                      : result.consecutive_growth_years >= 10
                      ? "#00C087"
                      : "#e2e8f0"
                  }
                  sub={
                    result.consecutive_growth_years >= 50
                      ? "Dividend King"
                      : result.consecutive_growth_years >= 25
                      ? "Aristocrat"
                      : undefined
                  }
                />
                {result.dividend_frequency != null && (
                  <MetricCard
                    label="派息频率"
                    value={`${result.dividend_frequency}×/年`}
                    color="#e2e8f0"
                  />
                )}
              </div>
            </div>
          )}

          {/* 年度历史柱图 */}
          {result.historical_annual.length > 0 && (
            <HistoricalBar entries={result.historical_annual} />
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
            <div>
              Payout Ratio &lt; 75% = 安全；75%-100% = 观察；≥ 100% = 危险（靠借债分红）
            </div>
            <div>
              股息捕获策略：Ex-Date 前 T-2 买入，获得股息后卖出；历史每次 0.5-3% alpha
            </div>
            <div>Dividend Aristocrat ≥ 25 年连续增长；Dividend King ≥ 50 年</div>
          </div>

          <div style={{ marginTop: 8, fontSize: 10, color: "#475569" }}>
            数据日期：{result.as_of_date}　│　来源：yfinance　│　不构成投资建议
          </div>
        </div>
      )}
    </div>
  );
}

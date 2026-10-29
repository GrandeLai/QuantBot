/**
 * BetaCorrelationPanel — 相关性与 Beta 监控面板
 *
 * 功能（需用户输入 ticker）：
 * - Beta 1Y / Beta 63D（vs SPY）
 * - 相关性 SPY / QQQ（-1 to 1）
 * - R²（系统性风险占比）
 * - 特质波动率（年化，剔除市场因子后残差）
 * - Beta 仪表条（0-3 水平条，1.0 为中性线）
 * - 信号徽章（high_beta 红、moderate_beta 橙、low_beta 绿、defensive 蓝）
 *
 * Phase F.31 — Correlation & Beta Monitor
 * 数据来源：yfinance 历史价格（SPY + QQQ + stock，免费）
 */

import { useState } from "react";
import {
  type BetaCorrelationData,
  type BetaSignal,
  fetchBetaCorrelation,
} from "../api/client";

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function signalColor(s: BetaSignal): string {
  if (s === "high_beta") return "#ef4444";
  if (s === "moderate_beta") return "#f59e0b";
  if (s === "low_beta") return "#00C087";
  if (s === "defensive") return "#60a5fa";
  return "#475569";
}

function signalLabel(s: BetaSignal): string {
  const labels: Record<BetaSignal, string> = {
    high_beta: "🔴 高 Beta（β ≥ 1.5）",
    moderate_beta: "⚠ 中等 Beta（1.0 ≤ β < 1.5）",
    low_beta: "✓ 低 Beta（0.5 ≤ β < 1.0）",
    defensive: "🛡 防御性（β < 0.5）",
    no_data: "— 无数据",
  };
  return labels[s];
}

function fmtNum(v: number | null, decimals = 2, suffix = ""): string {
  if (v == null) return "—";
  return `${v.toFixed(decimals)}${suffix}`;
}

function corrColor(v: number | null): string {
  if (v == null) return "#475569";
  if (v > 0.8) return "#ef4444";
  if (v > 0.6) return "#f59e0b";
  if (v < 0.3) return "#00C087";
  return "#94a3b8";
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

/** Beta bar: 0 to 3 scale with 1.0 center marker */
function BetaBar({ beta }: { beta: number | null }) {
  if (beta == null) return null;
  const clamped = Math.max(0, Math.min(3, beta));
  const pct = (clamped / 3) * 100;
  const color =
    beta >= 1.5 ? "#ef4444"
    : beta >= 1.0 ? "#f59e0b"
    : beta >= 0.5 ? "#00C087"
    : "#60a5fa";

  return (
    <div style={{ margin: "6px 0" }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 3, fontSize: 10 }}>
        <span style={{ color: "#64748b" }}>β 0</span>
        <span style={{ fontSize: 14, fontWeight: 700, color }}>β = {beta.toFixed(2)}</span>
        <span style={{ color: "#ef4444" }}>β 3</span>
      </div>
      <div style={{ position: "relative", background: "#1a1d24", borderRadius: 4, height: 10, overflow: "hidden" }}>
        {/* Center marker at β=1.0 (33%) */}
        <div
          style={{
            position: "absolute",
            left: "33.3%",
            top: 0,
            bottom: 0,
            width: 2,
            background: "#2a2d35",
          }}
        />
        <div
          style={{
            position: "absolute",
            top: 1,
            bottom: 1,
            left: 0,
            width: `${pct}%`,
            background: color,
            borderRadius: 3,
            transition: "width 0.4s",
          }}
        />
      </div>
      <div style={{ fontSize: 9, color: "#475569", marginTop: 1 }}>
        市场 Beta=1.0（33% 处）
      </div>
    </div>
  );
}

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
      <div style={{ fontSize: 14, fontWeight: 700, color: color ?? "#e2e8f0" }}>{value}</div>
      {sub && <div style={{ fontSize: 9, color: "#64748b", marginTop: 1 }}>{sub}</div>}
      <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>{label}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export default function BetaCorrelationPanel() {
  const [ticker, setTicker] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<BetaCorrelationData | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!ticker.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const data = await fetchBetaCorrelation(ticker.trim().toUpperCase());
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
        <span>相关性与 Beta 监控</span>
        <span style={{ fontSize: 10, color: "#475569", fontWeight: 400 }}>
          Beta · R² · 相关性 · 特质波动率
        </span>
      </div>

      {/* 输入 */}
      <form onSubmit={handleSubmit} style={{ display: "flex", gap: 8, marginBottom: 12 }}>
        <input
          value={ticker}
          onChange={(e) => setTicker(e.target.value.toUpperCase())}
          placeholder="Ticker（如 AAPL、NVDA、BRK-B）"
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

          {/* Beta 仪表条 */}
          {result.beta_1y != null && (
            <div
              style={{
                background: "#151619",
                border: "1px solid #2a2d35",
                borderRadius: 8,
                padding: "10px 14px",
                marginBottom: 10,
              }}
            >
              <BetaBar beta={result.beta_1y} />
              {result.beta_63d != null && (
                <div style={{ marginTop: 6, fontSize: 10, color: "#64748b" }}>
                  近期 Beta(63D)：
                  <span style={{ color: "#e2e8f0", fontWeight: 600 }}>
                    {result.beta_63d.toFixed(2)}
                  </span>
                  {result.beta_1y != null && (
                    <span
                      style={{
                        marginLeft: 6,
                        color:
                          result.beta_63d > result.beta_1y + 0.1 ? "#ef4444"
                          : result.beta_63d < result.beta_1y - 0.1 ? "#00C087"
                          : "#64748b",
                      }}
                    >
                      ({result.beta_63d > result.beta_1y + 0.1 ? "↑ 风险升" : result.beta_63d < result.beta_1y - 0.1 ? "↓ 风险降" : "≈ 稳定"})
                    </span>
                  )}
                </div>
              )}
            </div>
          )}

          {/* 指标卡片 */}
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
                label="相关性 vs SPY"
                value={fmtNum(result.corr_spy_1y)}
                color={corrColor(result.corr_spy_1y)}
                sub={result.corr_spy_1y != null ? (result.corr_spy_1y > 0.8 ? "高度相关" : result.corr_spy_1y < 0.3 ? "低相关" : "中等") : undefined}
              />
              <MetricCard
                label="相关性 vs QQQ"
                value={fmtNum(result.corr_qqq_1y)}
                color={corrColor(result.corr_qqq_1y)}
                sub={result.corr_qqq_1y != null && result.corr_qqq_1y > result.corr_spy_1y! + 0.1 ? "科技成分高" : undefined}
              />
              <MetricCard
                label="R²（系统风险）"
                value={fmtNum(result.r_squared_1y, 2)}
                color={
                  result.r_squared_1y != null && result.r_squared_1y > 0.7 ? "#ef4444"
                  : result.r_squared_1y != null && result.r_squared_1y < 0.3 ? "#00C087"
                  : "#e2e8f0"
                }
                sub={result.r_squared_1y != null ? `${(result.r_squared_1y * 100).toFixed(0)}% 市场驱动` : undefined}
              />
              {result.idio_vol_ann != null && (
                <MetricCard
                  label="特质波动率"
                  value={fmtNum(result.idio_vol_ann * 100, 1, "%")}
                  color="#94a3b8"
                  sub="年化（剔市场因子）"
                />
              )}
            </div>
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
            <div>Beta = Cov(stock, SPY) / Var(SPY)；β＞1.5 = 高杠杆市场暴露</div>
            <div>R² = 相关性²；R²高 = 市场主导；R²低 = 特质性 Alpha 机会</div>
            <div>特质波动率 = 剔除市场因子后的残差风险（Ang et al. 2006）</div>
          </div>

          <div style={{ marginTop: 8, fontSize: 10, color: "#475569" }}>
            数据日期：{result.as_of_date}　│　Benchmark: SPY & QQQ（1Y）　│　不构成投资建议
          </div>
        </div>
      )}
    </div>
  );
}

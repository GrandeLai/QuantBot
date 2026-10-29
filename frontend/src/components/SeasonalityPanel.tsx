/**
 * SeasonalityPanel — 季节性模式分析面板
 *
 * 功能（需用户输入 ticker）：
 * - 当前月份信号徽章（strong_season / positive_season / neutral / negative_season / strong_negative）
 * - 12 个月月度热图（颜色深浅表示历史平均收益率高低）
 * - 当月详情（avg / median / 正收益率 / 样本量）
 * - 最强 / 最弱月份标注
 *
 * Phase F.32 — Seasonality Pattern Analysis
 * 数据来源：yfinance 月度历史（最多 10 年，免费）
 */

import { useState } from "react";
import {
  type MonthStats,
  type SeasonalSignal,
  type SeasonalityData,
  fetchSeasonality,
} from "../api/client";

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function signalColor(s: SeasonalSignal): string {
  if (s === "strong_season") return "#00C087";
  if (s === "positive_season") return "#34d399";
  if (s === "neutral") return "#94a3b8";
  if (s === "negative_season") return "#f59e0b";
  if (s === "strong_negative") return "#ef4444";
  return "#475569";
}

function signalLabel(s: SeasonalSignal): string {
  const labels: Record<SeasonalSignal, string> = {
    strong_season: "🟢 强季节性（月均 >3%）",
    positive_season: "✅ 正季节性（月均 1-3%）",
    neutral: "⚪ 中性（月均 -1%~1%）",
    negative_season: "⚠ 负季节性（月均 -3%~-1%）",
    strong_negative: "🔴 强负季节性（月均 ≤-3%）",
    no_data: "— 无数据",
  };
  return labels[s];
}

function fmtPct(v: number, decimals = 1): string {
  return `${(v * 100).toFixed(decimals)}%`;
}

/** Map avg_return to a background color for the heatmap cell. */
function heatmapColor(avg: number, hasSamples: boolean): string {
  if (!hasSamples) return "#1a1d24";
  if (avg > 0.03) return "#064e3b";
  if (avg > 0.01) return "#065f46";
  if (avg > 0) return "#0f3028";
  if (avg > -0.01) return "#3b2108";
  if (avg > -0.03) return "#7c2d12";
  return "#991b1b";
}

function heatmapTextColor(avg: number, hasSamples: boolean): string {
  if (!hasSamples) return "#475569";
  if (avg > 0) return "#6ee7b7";
  return "#fca5a5";
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function MonthCell({
  stats,
  isCurrent,
  isBest,
  isWorst,
}: {
  stats: MonthStats;
  isCurrent: boolean;
  isBest: boolean;
  isWorst: boolean;
}) {
  const hasSamples = stats.sample_size > 0;
  const bg = heatmapColor(stats.avg_return, hasSamples);
  const textColor = heatmapTextColor(stats.avg_return, hasSamples);

  return (
    <div
      style={{
        background: bg,
        border: isCurrent
          ? "2px solid #60a5fa"
          : "1px solid #2a2d35",
        borderRadius: 6,
        padding: "6px 4px",
        textAlign: "center",
        position: "relative",
        minWidth: 60,
      }}
    >
      {/* Badge row */}
      <div style={{ fontSize: 9, marginBottom: 2, height: 12 }}>
        {isBest && <span style={{ color: "#00C087" }}>★</span>}
        {isWorst && <span style={{ color: "#ef4444" }}>▼</span>}
        {isCurrent && !isBest && !isWorst && (
          <span style={{ color: "#60a5fa" }}>●</span>
        )}
      </div>

      {/* Month abbrev */}
      <div style={{ fontSize: 10, color: "#94a3b8", marginBottom: 2 }}>
        {stats.month_name.slice(0, 3)}
      </div>

      {/* Avg return */}
      <div
        style={{
          fontSize: 12,
          fontWeight: 700,
          color: hasSamples ? textColor : "#475569",
        }}
      >
        {hasSamples ? fmtPct(stats.avg_return) : "—"}
      </div>

      {/* Sample size */}
      <div style={{ fontSize: 9, color: "#475569", marginTop: 1 }}>
        {hasSamples ? `n=${stats.sample_size}` : ""}
      </div>
    </div>
  );
}

function CurrentMonthDetail({ stats }: { stats: MonthStats }) {
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
      <div
        style={{
          fontSize: 11,
          color: "#64748b",
          marginBottom: 8,
          fontWeight: 600,
          textTransform: "uppercase",
          letterSpacing: 0.5,
        }}
      >
        {stats.month_name} 历史详情
      </div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        {[
          { label: "平均收益", value: fmtPct(stats.avg_return), color: stats.avg_return >= 0 ? "#00C087" : "#ef4444" },
          { label: "中位收益", value: fmtPct(stats.median_return), color: stats.median_return >= 0 ? "#00C087" : "#ef4444" },
          { label: "正收益率", value: fmtPct(stats.positive_rate, 0), color: "#e2e8f0" },
          { label: "样本量（年）", value: String(stats.sample_size), color: "#e2e8f0" },
        ].map(({ label, value, color }) => (
          <div
            key={label}
            style={{
              flex: 1,
              background: "#0E1014",
              border: "1px solid #2a2d35",
              borderRadius: 5,
              padding: "7px 8px",
              textAlign: "center",
              minWidth: 70,
            }}
          >
            <div style={{ fontSize: 14, fontWeight: 700, color }}>{value}</div>
            <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>{label}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export default function SeasonalityPanel() {
  const [ticker, setTicker] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<SeasonalityData | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!ticker.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const data = await fetchSeasonality(ticker.trim().toUpperCase());
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
        <span>季节性模式分析</span>
        <span style={{ fontSize: 10, color: "#475569", fontWeight: 400 }}>
          月度历史统计 · 最多 10 年
        </span>
      </div>

      {/* 输入 */}
      <form onSubmit={handleSubmit} style={{ display: "flex", gap: 8, marginBottom: 12 }}>
        <input
          value={ticker}
          onChange={(e) => setTicker(e.target.value.toUpperCase())}
          placeholder="Ticker（如 AAPL、SPY、QQQ）"
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

          {/* 12 个月热图 */}
          {result.all_months.length > 0 && (
            <div
              style={{
                background: "#151619",
                border: "1px solid #2a2d35",
                borderRadius: 8,
                padding: "10px 14px",
                marginBottom: 10,
              }}
            >
              <div
                style={{
                  fontSize: 11,
                  color: "#64748b",
                  marginBottom: 8,
                  fontWeight: 600,
                  textTransform: "uppercase",
                  letterSpacing: 0.5,
                }}
              >
                月度历史收益热图
              </div>
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(6, 1fr)",
                  gap: 4,
                }}
              >
                {result.all_months.map((stats) => (
                  <MonthCell
                    key={stats.month}
                    stats={stats}
                    isCurrent={stats.month === result.current_month}
                    isBest={stats.month === result.best_month}
                    isWorst={stats.month === result.worst_month}
                  />
                ))}
              </div>
              {/* 图例 */}
              <div
                style={{
                  display: "flex",
                  gap: 12,
                  marginTop: 8,
                  fontSize: 9,
                  color: "#475569",
                  flexWrap: "wrap",
                }}
              >
                <span><span style={{ color: "#60a5fa" }}>●</span> 当前月份</span>
                <span><span style={{ color: "#00C087" }}>★</span> 历史最强月</span>
                <span><span style={{ color: "#ef4444" }}>▼</span> 历史最弱月</span>
                <span style={{ color: "#6ee7b7" }}>绿色 = 正收益</span>
                <span style={{ color: "#fca5a5" }}>红色 = 负收益</span>
              </div>
            </div>
          )}

          {/* 当月详情 */}
          {result.current_month_stats && (
            <CurrentMonthDetail stats={result.current_month_stats} />
          )}

          {/* 最强/最弱月 */}
          {(result.best_month || result.worst_month) && result.all_months.length > 0 && (
            <div
              style={{
                background: "#151619",
                border: "1px solid #2a2d35",
                borderRadius: 8,
                padding: "10px 14px",
                marginBottom: 10,
                display: "flex",
                gap: 12,
              }}
            >
              {result.best_month && (() => {
                const best = result.all_months.find(s => s.month === result.best_month);
                return best ? (
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: 10, color: "#00C087", marginBottom: 2 }}>★ 历史最强月</div>
                    <div style={{ fontSize: 13, fontWeight: 700, color: "#e2e8f0" }}>
                      {best.month_name}
                    </div>
                    <div style={{ fontSize: 11, color: "#6ee7b7" }}>
                      均 {fmtPct(best.avg_return)} · 正收益率 {fmtPct(best.positive_rate, 0)}
                    </div>
                  </div>
                ) : null;
              })()}
              {result.worst_month && (() => {
                const worst = result.all_months.find(s => s.month === result.worst_month);
                return worst ? (
                  <div style={{ flex: 1, borderLeft: "1px solid #2a2d35", paddingLeft: 12 }}>
                    <div style={{ fontSize: 10, color: "#ef4444", marginBottom: 2 }}>▼ 历史最弱月</div>
                    <div style={{ fontSize: 13, fontWeight: 700, color: "#e2e8f0" }}>
                      {worst.month_name}
                    </div>
                    <div style={{ fontSize: 11, color: "#fca5a5" }}>
                      均 {fmtPct(worst.avg_return)} · 正收益率 {fmtPct(worst.positive_rate, 0)}
                    </div>
                  </div>
                ) : null;
              })()}
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

          <div style={{ fontSize: 10, color: "#475569" }}>
            数据日期：{result.as_of_date}　│　来源：yfinance 月度数据（最多 10 年）　│　不构成投资建议
          </div>
        </div>
      )}
    </div>
  );
}

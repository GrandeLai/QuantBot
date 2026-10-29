/**
 * FundamentalPanel — 基本面 Alpha 面板（PEAD + Piotroski F-Score）
 *
 * 功能：
 * - PEAD（Post-Earnings Announcement Drift）：展示最新 EPS surprise 和历史漂移估算
 * - Piotroski F-Score：展示 9 维财务健康评分和明细信号
 *
 * Phase F.4 — #0E1014 / #151619 / #00C087 dark-theme tokens
 */
import React, { useState } from "react";
import {
  type FundamentalSummary,
  type PEADSignal,
  type PiotroskiScore,
  type SurpriseMagnitude,
  fetchFundamentalSummary,
} from "../api/client";

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function magnitudeLabel(m: SurpriseMagnitude): string {
  const labels: Record<SurpriseMagnitude, string> = {
    large_beat: "大幅超预期",
    beat: "超预期",
    inline: "符合预期",
    miss: "低于预期",
    large_miss: "大幅低于预期",
  };
  return labels[m] ?? m;
}

function magnitudeColor(m: SurpriseMagnitude): string {
  if (m === "large_beat" || m === "beat") return "#00C087";
  if (m === "large_miss" || m === "miss") return "#ef4444";
  return "#94a3b8";
}

function gradeColor(g: "strong" | "moderate" | "weak"): string {
  if (g === "strong") return "#00C087";
  if (g === "weak") return "#ef4444";
  return "#f59e0b";
}

function formatDrift(val: number | null): string {
  if (val === null) return "—";
  const sign = val >= 0 ? "+" : "";
  return `${sign}${val.toFixed(2)}%`;
}

function formatSurprisePct(pct: number): string {
  const sign = pct >= 0 ? "+" : "";
  return `${sign}${(pct * 100).toFixed(2)}%`;
}

// ---------------------------------------------------------------------------
// 子组件：信号强度进度条
// ---------------------------------------------------------------------------

function SignalBar({ value, color = "#00C087" }: { value: number; color?: string }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        fontSize: 12,
      }}
    >
      <div
        style={{
          flex: 1,
          height: 6,
          background: "#2a2d35",
          borderRadius: 3,
          overflow: "hidden",
        }}
      >
        <div
          style={{
            width: `${Math.round(value * 100)}%`,
            height: "100%",
            background: color,
            borderRadius: 3,
            transition: "width 0.3s ease",
          }}
        />
      </div>
      <span style={{ color, minWidth: 36, textAlign: "right" }}>
        {(value * 100).toFixed(0)}%
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 子组件：PEAD 面板
// ---------------------------------------------------------------------------

function PEADSection({ pead }: { pead: PEADSignal }) {
  const s = pead.latest_surprise;
  const mc = magnitudeColor(pead.surprise_magnitude);

  return (
    <div
      style={{
        background: "#151619",
        border: "1px solid #2a2d35",
        borderRadius: 8,
        padding: "12px 14px",
        marginBottom: 10,
      }}
    >
      <div
        style={{
          fontSize: 12,
          fontWeight: 700,
          color: "#94a3b8",
          marginBottom: 10,
          textTransform: "uppercase",
          letterSpacing: 1,
        }}
      >
        PEAD — 盈利公告后漂移
      </div>

      {/* 最新 EPS surprise */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          marginBottom: 8,
        }}
      >
        <div>
          <div style={{ fontSize: 11, color: "#64748b", marginBottom: 2 }}>
            最新季度：{s.quarter}
          </div>
          <div style={{ fontSize: 13, color: "#e2e8f0" }}>
            实际 EPS:{" "}
            <strong>${s.eps_actual.toFixed(2)}</strong>
            {"  "}
            <span style={{ color: "#64748b" }}>
              预期 ${s.eps_estimate.toFixed(2)}
            </span>
          </div>
        </div>
        <div style={{ textAlign: "right" }}>
          <div style={{ fontSize: 16, fontWeight: 700, color: mc }}>
            {formatSurprisePct(s.surprise_pct)}
          </div>
          <div
            style={{
              fontSize: 11,
              color: mc,
              background: mc + "22",
              padding: "2px 7px",
              borderRadius: 4,
              marginTop: 2,
            }}
          >
            {magnitudeLabel(pead.surprise_magnitude)}
          </div>
        </div>
      </div>

      {/* 信号强度 */}
      <div style={{ marginBottom: 8 }}>
        <div style={{ fontSize: 11, color: "#64748b", marginBottom: 3 }}>
          信号强度
        </div>
        <SignalBar value={pead.signal_strength} color={mc} />
      </div>

      {/* 历史漂移 */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr 1fr",
          gap: 6,
          marginTop: 8,
        }}
      >
        {([
          ["7 日", pead.historical_drift_7d],
          ["30 日", pead.historical_drift_30d],
          ["60 日", pead.historical_drift_60d],
        ] as [string, number | null][]).map(([label, val]) => {
          const color =
            val === null
              ? "#64748b"
              : val >= 0
              ? "#00C087"
              : "#ef4444";
          return (
            <div
              key={label}
              style={{
                background: "#0E1014",
                border: "1px solid #2a2d35",
                borderRadius: 6,
                padding: "6px 10px",
                textAlign: "center",
              }}
            >
              <div style={{ fontSize: 10, color: "#64748b", marginBottom: 2 }}>
                {label}漂移
              </div>
              <div style={{ fontSize: 14, fontWeight: 600, color }}>
                {formatDrift(val)}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 子组件：Piotroski 面板
// ---------------------------------------------------------------------------

const _SIGNAL_LABELS: Record<string, string> = {
  F1_roa_positive: "ROA > 0",
  F2_operating_cashflow_positive: "经营现金流 > 0",
  F3_roa_improving: "ROA 改善",
  F4_accruals_low: "应计项目质量好",
  F5_leverage_decreasing: "杠杆率降低",
  F6_current_ratio_improving: "流动性改善",
  F7_no_dilution: "无稀释增发",
  F8_gross_margin_improving: "毛利率改善",
  F9_asset_turnover_improving: "资产周转率改善",
};

function PiotroskiSection({ ps }: { ps: PiotroskiScore }) {
  const gc = gradeColor(ps.grade);
  const gradeLabels = { strong: "优质", moderate: "中等", weak: "红旗" };

  return (
    <div
      style={{
        background: "#151619",
        border: "1px solid #2a2d35",
        borderRadius: 8,
        padding: "12px 14px",
      }}
    >
      <div
        style={{
          fontSize: 12,
          fontWeight: 700,
          color: "#94a3b8",
          marginBottom: 10,
          textTransform: "uppercase",
          letterSpacing: 1,
        }}
      >
        Piotroski F-Score — 财务健康评分
      </div>

      {/* 总评分 */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 10,
        }}
      >
        <div>
          <span style={{ fontSize: 28, fontWeight: 700, color: gc }}>
            {ps.score}
          </span>
          <span style={{ fontSize: 14, color: "#64748b", marginLeft: 4 }}>
            / 9
          </span>
          <span
            style={{
              marginLeft: 10,
              fontSize: 12,
              padding: "2px 8px",
              borderRadius: 4,
              background: gc + "22",
              color: gc,
            }}
          >
            {gradeLabels[ps.grade]}
          </span>
        </div>
        <div style={{ fontSize: 11, color: "#64748b", maxWidth: 220, textAlign: "right", lineHeight: 1.4 }}>
          {ps.interpretation}
        </div>
      </div>

      {/* 评分条 */}
      <div style={{ marginBottom: 12 }}>
        <SignalBar value={ps.score / 9} color={gc} />
      </div>

      {/* 9 个信号明细 */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr 1fr",
          gap: 5,
        }}
      >
        {Object.entries(ps.signals).map(([key, val]) => (
          <div
            key={key}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 5,
              background: "#0E1014",
              border: `1px solid ${val ? "#00C08733" : "#ef444433"}`,
              borderRadius: 5,
              padding: "4px 8px",
              fontSize: 11,
              color: val ? "#00C087" : "#ef4444",
            }}
          >
            <span>{val ? "✓" : "✗"}</span>
            <span style={{ color: "#e2e8f0" }}>
              {_SIGNAL_LABELS[key] ?? key}
            </span>
          </div>
        ))}
      </div>

      <div style={{ marginTop: 8, fontSize: 10, color: "#475569" }}>
        数据日期：{ps.as_of_date}　│　来源：yfinance　│　不构成投资建议
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export default function FundamentalPanel() {
  const [inputTicker, setInputTicker] = useState("AAPL");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<FundamentalSummary | null>(null);

  async function handleQuery() {
    if (!inputTicker.trim()) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const data = await fetchFundamentalSummary(inputTicker.trim().toUpperCase());
      setResult(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "查询失败");
    } finally {
      setLoading(false);
    }
  }

  const inputStyle: React.CSSProperties = {
    background: "#0E1014",
    border: "1px solid #2a2d35",
    borderRadius: 4,
    color: "#e2e8f0",
    padding: "6px 10px",
    fontSize: 13,
    outline: "none",
    width: 120,
  };

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
        <span>基本面信号（PEAD + Piotroski F-Score）</span>
        <span style={{ fontSize: 10, color: "#475569", fontWeight: 400 }}>
          数据来源：yfinance
        </span>
      </div>

      {/* 查询栏 */}
      <div
        style={{
          display: "flex",
          gap: 8,
          marginBottom: 14,
          alignItems: "center",
        }}
      >
        <input
          value={inputTicker}
          onChange={(e) => setInputTicker(e.target.value.toUpperCase())}
          onKeyDown={(e) => e.key === "Enter" && handleQuery()}
          placeholder="AAPL"
          style={inputStyle}
        />
        <button
          onClick={handleQuery}
          disabled={loading}
          style={{
            background: loading ? "#1f2937" : "#00C087",
            color: loading ? "#94a3b8" : "#0E1014",
            border: "none",
            borderRadius: 6,
            padding: "6px 18px",
            fontWeight: 700,
            fontSize: 13,
            cursor: loading ? "not-allowed" : "pointer",
          }}
        >
          {loading ? "查询中..." : "查询"}
        </button>
      </div>

      {/* 错误提示 */}
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

      {/* 结果区 */}
      {result && (
        <>
          {result.pead ? (
            <PEADSection pead={result.pead} />
          ) : (
            <div
              style={{
                background: "#151619",
                border: "1px solid #2a2d35",
                borderRadius: 8,
                padding: "10px 14px",
                marginBottom: 10,
                fontSize: 12,
                color: "#64748b",
              }}
            >
              PEAD：暂无 EPS 数据（非美股 or yfinance 覆盖不足）
            </div>
          )}

          {result.piotroski ? (
            <PiotroskiSection ps={result.piotroski} />
          ) : (
            <div
              style={{
                background: "#151619",
                border: "1px solid #2a2d35",
                borderRadius: 8,
                padding: "10px 14px",
                fontSize: 12,
                color: "#64748b",
              }}
            >
              Piotroski：暂无财务报表数据（非美股 or yfinance 覆盖不足）
            </div>
          )}
        </>
      )}

      {/* 空态提示 */}
      {!result && !loading && !error && (
        <div
          style={{
            textAlign: "center",
            color: "#475569",
            fontSize: 12,
            padding: "20px 0",
          }}
        >
          输入股票代码后点击"查询"，获取 PEAD 和 Piotroski F-Score 评分
        </div>
      )}
    </div>
  );
}

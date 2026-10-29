/**
 * PEADPanel — Post-Earnings Announcement Drift 盈利公告后漂移面板
 *
 * 功能：
 * - 展示最近一次财报的实际 vs 预期 EPS 及惊喜幅度
 * - 基于 Bernard & Thomas (1989) 学术研究给出 30/60/90 日预期漂移
 * - 显示下次财报预计日期
 *
 * Phase F.13 — #0E1014 / #151619 / #00C087 dark-theme tokens
 */
import React, { useState } from "react";
import {
  type EarningsEventData,
  type EarningsSurpriseGrade,
  type PEADSignalData,
  fetchPEADSignal,
} from "../api/client";

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function gradeColor(grade: EarningsSurpriseGrade): string {
  if (grade === "large_beat") return "#00C087";
  if (grade === "beat") return "#34d399";
  if (grade === "inline") return "#64748b";
  if (grade === "miss") return "#f59e0b";
  return "#ef4444"; // large_miss
}

function gradeLabel(grade: EarningsSurpriseGrade): string {
  const labels: Record<EarningsSurpriseGrade, string> = {
    large_beat: "大幅超预期 ✓✓",
    beat: "超预期 ✓",
    inline: "符合预期",
    miss: "低于预期 ⚠",
    large_miss: "大幅低于预期 ⚠⚠",
  };
  return labels[grade];
}

function driftColor(drift: number): string {
  if (drift > 2) return "#00C087";
  if (drift > 0) return "#34d399";
  if (drift < -2) return "#ef4444";
  if (drift < 0) return "#f59e0b";
  return "#64748b";
}

function formatDrift(drift: number): string {
  return drift >= 0 ? `+${drift.toFixed(1)}%` : `${drift.toFixed(1)}%`;
}

function daysUntil(dateStr: string): number | null {
  try {
    const target = new Date(dateStr);
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const diff = Math.round((target.getTime() - today.getTime()) / 86_400_000);
    return diff;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// 子组件：单次财报事件卡
// ---------------------------------------------------------------------------

function EarningsCard({ event }: { event: EarningsEventData }) {
  const gc = gradeColor(event.grade);
  const gl = gradeLabel(event.grade);
  const spSign = event.surprise_pct >= 0 ? "+" : "";

  return (
    <div
      style={{
        background: "#0E1014",
        border: `1px solid ${gc}44`,
        borderRadius: 8,
        padding: "10px 14px",
        marginBottom: 10,
      }}
    >
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
            财报日：{event.earnings_date}
          </div>
          <span
            style={{
              fontSize: 12,
              padding: "2px 10px",
              borderRadius: 4,
              background: gc + "22",
              color: gc,
              fontWeight: 700,
            }}
          >
            {gl}
          </span>
        </div>
        <div style={{ textAlign: "right" }}>
          <span style={{ fontSize: 28, fontWeight: 700, color: gc }}>
            {spSign}{event.surprise_pct.toFixed(1)}%
          </span>
          <div style={{ fontSize: 10, color: "#64748b" }}>EPS 惊喜幅度</div>
        </div>
      </div>

      {/* Actual vs Estimated */}
      <div style={{ display: "flex", gap: 8 }}>
        {[
          { label: "实际 EPS", value: event.actual_eps, color: gc },
          { label: "预期 EPS", value: event.estimated_eps, color: "#94a3b8" },
        ].map(({ label, value, color }) => (
          <div
            key={label}
            style={{
              flex: 1,
              background: "#151619",
              border: "1px solid #2a2d35",
              borderRadius: 5,
              padding: "5px 8px",
              textAlign: "center",
            }}
          >
            <div style={{ fontSize: 15, fontWeight: 700, color }}>
              {value >= 0 ? "" : "−"}${Math.abs(value).toFixed(2)}
            </div>
            <div style={{ fontSize: 9, color: "#475569", marginTop: 1 }}>
              {label}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 子组件：漂移预期条
// ---------------------------------------------------------------------------

function DriftBar({ label, drift }: { label: string; drift: number }) {
  const color = driftColor(drift);
  // Map -10% to +10% onto 0-100%
  const RANGE = 10;
  const pct = ((drift + RANGE) / (2 * RANGE)) * 100;
  const clampedPct = Math.max(0, Math.min(100, pct));

  return (
    <div style={{ marginBottom: 8 }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          fontSize: 11,
          color: "#94a3b8",
          marginBottom: 3,
        }}
      >
        <span>{label}</span>
        <span style={{ color, fontWeight: 700 }}>{formatDrift(drift)}</span>
      </div>
      <div
        style={{
          position: "relative",
          height: 6,
          background: "#2a2d35",
          borderRadius: 3,
        }}
      >
        {/* Zero center line */}
        <div
          style={{
            position: "absolute",
            left: "50%",
            top: 0,
            bottom: 0,
            width: 1,
            background: "#475569",
          }}
        />
        {/* Indicator dot */}
        <div
          style={{
            position: "absolute",
            left: `${clampedPct}%`,
            top: "50%",
            transform: "translate(-50%, -50%)",
            width: 10,
            height: 10,
            borderRadius: "50%",
            background: color,
            boxShadow: `0 0 4px ${color}88`,
          }}
        />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export default function PEADPanel() {
  const [inputTicker, setInputTicker] = useState("AAPL");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<PEADSignalData | null>(null);

  async function handleQuery() {
    if (!inputTicker.trim()) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const data = await fetchPEADSignal(inputTicker.trim().toUpperCase());
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
        <span>PEAD — 盈利公告后漂移信号</span>
        <span style={{ fontSize: 10, color: "#475569", fontWeight: 400 }}>
          来源：yfinance · 漂移参考：Bernard & Thomas 1989
        </span>
      </div>

      {/* 查询栏 */}
      <div
        style={{ display: "flex", gap: 8, marginBottom: 14, alignItems: "center" }}
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
          {/* 财报事件卡 */}
          <EarningsCard event={result.last_earnings} />

          {/* 解读 */}
          <div
            style={{
              background: "#151619",
              border: "1px solid #2a2d35",
              borderRadius: 6,
              padding: "8px 12px",
              fontSize: 11,
              color: "#94a3b8",
              lineHeight: 1.5,
              marginBottom: 12,
            }}
          >
            {result.interpretation}
          </div>

          {/* 漂移预期 */}
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
                fontWeight: 700,
                color: "#94a3b8",
                marginBottom: 10,
                textTransform: "uppercase",
                letterSpacing: 1,
              }}
            >
              学术研究预期漂移（%）
            </div>
            <DriftBar label="30 日预期漂移" drift={result.expected_drift_30d} />
            <DriftBar label="60 日预期漂移" drift={result.expected_drift_60d} />
            <DriftBar label="90 日预期漂移" drift={result.expected_drift_90d} />
            <div
              style={{
                fontSize: 9,
                color: "#475569",
                marginTop: 6,
                lineHeight: 1.5,
              }}
            >
              参考值来自 Bernard & Thomas (1989)、Livnat & Mendenhall (2006)。
              实际漂移因市值/行业/流动性而异，仅供参考，不构成投资建议。
            </div>
          </div>

          {/* 下次财报日 */}
          {result.next_earnings_date && (
            <div
              style={{
                background: "#151619",
                border: "1px solid #2a2d35",
                borderRadius: 8,
                padding: "8px 14px",
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                marginBottom: 10,
              }}
            >
              <div>
                <div style={{ fontSize: 11, color: "#64748b" }}>下次财报预计日期</div>
                <div style={{ fontSize: 14, fontWeight: 700, color: "#e2e8f0" }}>
                  {result.next_earnings_date}
                </div>
              </div>
              {(() => {
                const days = daysUntil(result.next_earnings_date);
                if (days === null) return null;
                const color = days <= 14 ? "#f59e0b" : "#64748b";
                return (
                  <div style={{ textAlign: "right" }}>
                    <div style={{ fontSize: 18, fontWeight: 700, color }}>
                      {days > 0 ? `${days} 天后` : days === 0 ? "今天" : "已过"}
                    </div>
                  </div>
                );
              })()}
            </div>
          )}

          <div style={{ fontSize: 10, color: "#475569" }}>
            数据日期：{result.as_of_date}
          </div>
        </div>
      )}

      {/* 空态 */}
      {!result && !loading && !error && (
        <div
          style={{
            textAlign: "center",
            color: "#475569",
            fontSize: 12,
            padding: "20px 0",
          }}
        >
          输入股票代码查询最近一次财报的 EPS 惊喜与 PEAD 预期漂移
        </div>
      )}
    </div>
  );
}

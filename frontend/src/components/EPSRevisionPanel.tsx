/**
 * EPSRevisionPanel — 分析师 EPS 预期修正动量面板
 *
 * 功能：
 * - 展示每个预测期（本季度/下季度/本年度/明年度）的向上/向下修正次数
 * - 计算 7 日 + 30 日修正动量评分
 * - 展示分析师目标价共识（均值/中位/高/低 + 上行空间）
 *
 * Phase F.6 — #0E1014 / #151619 / #00C087 dark-theme tokens
 */
import React, { useState } from "react";
import {
  type EpsRevisionMomentumData,
  type EpsRevisionPeriodData,
  fetchEpsRevisionSummary,
} from "../api/client";

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

type Direction = EpsRevisionPeriodData["direction"];

function directionColor(d: Direction): string {
  if (d === "strong_upgrade") return "#00C087";
  if (d === "upgrade") return "#34d399";
  if (d === "neutral") return "#64748b";
  if (d === "downgrade") return "#f59e0b";
  return "#ef4444"; // strong_downgrade
}

function directionLabel(d: Direction): string {
  const map: Record<Direction, string> = {
    strong_upgrade: "强力上调",
    upgrade: "上调",
    neutral: "中性",
    downgrade: "下调",
    strong_downgrade: "强力下调",
  };
  return map[d] ?? d;
}

function scoreBar(score: number): React.ReactElement {
  // score ∈ [-1, 1]  →  map to [0, 100]%
  const pct = Math.round(((score + 1) / 2) * 100);
  const color = score > 0.1 ? "#00C087" : score < -0.1 ? "#ef4444" : "#64748b";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11 }}>
      <div
        style={{
          flex: 1,
          height: 5,
          background: "#2a2d35",
          borderRadius: 3,
          overflow: "hidden",
          position: "relative",
        }}
      >
        {/* centre marker */}
        <div
          style={{
            position: "absolute",
            left: "50%",
            top: 0,
            width: 1,
            height: "100%",
            background: "#475569",
          }}
        />
        <div
          style={{
            position: "absolute",
            left: score >= 0 ? "50%" : `${pct}%`,
            width: score >= 0 ? `${pct - 50}%` : `${50 - pct}%`,
            height: "100%",
            background: color,
            borderRadius: 3,
          }}
        />
      </div>
      <span style={{ color, minWidth: 40, textAlign: "right" }}>
        {score >= 0 ? "+" : ""}
        {(score * 100).toFixed(0)}%
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 子组件：修正行
// ---------------------------------------------------------------------------

function RevisionRow({ p }: { p: EpsRevisionPeriodData }) {
  const dc = directionColor(p.direction);
  const dl = directionLabel(p.direction);

  return (
    <div
      style={{
        background: "#0E1014",
        border: `1px solid ${dc}33`,
        borderRadius: 7,
        padding: "10px 12px",
        marginBottom: 6,
      }}
    >
      {/* 期间标题 + 方向徽章 */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 8,
        }}
      >
        <div style={{ fontWeight: 700, fontSize: 12, color: "#e2e8f0" }}>
          {p.period_label}
          <span style={{ marginLeft: 6, fontSize: 10, color: "#64748b" }}>
            ({p.period})
          </span>
        </div>
        <div
          style={{
            fontSize: 11,
            padding: "2px 8px",
            borderRadius: 4,
            background: dc + "22",
            color: dc,
            fontWeight: 700,
          }}
        >
          {dl}
        </div>
      </div>

      {/* 7 日修正 */}
      <div style={{ marginBottom: 5 }}>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            fontSize: 10,
            color: "#64748b",
            marginBottom: 3,
          }}
        >
          <span>7 日修正</span>
          <span>
            <span style={{ color: "#00C087" }}>↑{p.up_7d}</span>
            {"  "}
            <span style={{ color: "#ef4444" }}>↓{p.down_7d}</span>
          </span>
        </div>
        {scoreBar(p.revision_score_7d)}
      </div>

      {/* 30 日修正 */}
      <div>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            fontSize: 10,
            color: "#64748b",
            marginBottom: 3,
          }}
        >
          <span>30 日修正</span>
          <span>
            <span style={{ color: "#00C087" }}>↑{p.up_30d}</span>
            {"  "}
            <span style={{ color: "#ef4444" }}>↓{p.down_30d}</span>
          </span>
        </div>
        {scoreBar(p.revision_score_30d)}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 子组件：目标价共识
// ---------------------------------------------------------------------------

function TargetSection({ data }: { data: EpsRevisionMomentumData }) {
  const t = data.targets;
  if (!t || t.target_mean == null) return null;

  const upside = t.upside_pct ?? 0;
  const upsideColor =
    upside > 0.1 ? "#00C087" : upside < -0.1 ? "#ef4444" : "#64748b";
  const upPct = Math.max(0, Math.min(100, (upside + 0.5) * 100));

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
        分析师目标价共识
      </div>

      {/* 价格网格 */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: 6,
          marginBottom: 12,
        }}
      >
        {[
          ["均值", t.target_mean],
          ["中位数", t.target_median],
          ["最高", t.target_high],
          ["最低", t.target_low],
        ].map(([label, val]) => (
          <div
            key={label as string}
            style={{
              background: "#0E1014",
              border: "1px solid #2a2d35",
              borderRadius: 5,
              padding: "6px 10px",
            }}
          >
            <div style={{ fontSize: 10, color: "#64748b" }}>{label as string}</div>
            <div style={{ fontSize: 14, fontWeight: 600, color: "#e2e8f0" }}>
              {val != null ? `$${(val as number).toFixed(2)}` : "—"}
            </div>
          </div>
        ))}
      </div>

      {/* 上行空间 */}
      <div>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            fontSize: 11,
            color: "#64748b",
            marginBottom: 4,
          }}
        >
          <span>上行空间（均值 vs 现价）</span>
          <span style={{ color: upsideColor, fontWeight: 700 }}>
            {upside >= 0 ? "+" : ""}
            {(upside * 100).toFixed(1)}%
          </span>
        </div>
        <div
          style={{
            height: 6,
            background: "#2a2d35",
            borderRadius: 3,
            overflow: "hidden",
          }}
        >
          <div
            style={{
              width: `${upPct}%`,
              height: "100%",
              background: upsideColor,
              borderRadius: 3,
              transition: "width 0.3s ease",
            }}
          />
        </div>
      </div>

      {t.current_price && (
        <div style={{ marginTop: 8, fontSize: 10, color: "#475569" }}>
          现价：${t.current_price.toFixed(2)}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export default function EPSRevisionPanel() {
  const [inputTicker, setInputTicker] = useState("AAPL");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<EpsRevisionMomentumData | null>(null);

  async function handleQuery() {
    if (!inputTicker.trim()) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const data = await fetchEpsRevisionSummary(inputTicker.trim().toUpperCase());
      setResult(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "查询失败");
    } finally {
      setLoading(false);
    }
  }

  const overallColor = result ? directionColor(result.overall_direction) : "#64748b";
  const overallLabel = result ? directionLabel(result.overall_direction) : "";

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
        <span>EPS 修正动量（Analyst Revision Momentum）</span>
        {result && (
          <span
            style={{
              fontSize: 11,
              padding: "2px 8px",
              borderRadius: 4,
              background: overallColor + "22",
              color: overallColor,
              fontWeight: 700,
            }}
          >
            总评：{overallLabel}
          </span>
        )}
      </div>

      {/* 查询栏 */}
      <div style={{ display: "flex", gap: 8, marginBottom: 14, alignItems: "center" }}>
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
        <>
          {/* 修正动量区块 */}
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
              分析师修正动量（↑上调 / ↓下调）
            </div>
            {result.periods.map((p) => (
              <RevisionRow key={p.period} p={p} />
            ))}
          </div>

          {/* 目标价共识 */}
          <TargetSection data={result} />

          <div style={{ marginTop: 8, fontSize: 10, color: "#475569" }}>
            数据日期：{result.as_of_date}　│　来源：yfinance analyst data　│　不构成投资建议
          </div>
        </>
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
          输入股票代码后点击"查询"，获取 EPS 修正动量和目标价共识
        </div>
      )}
    </div>
  );
}

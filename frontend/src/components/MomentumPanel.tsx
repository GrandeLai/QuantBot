/**
 * MomentumPanel — Jegadeesh-Titman 价格动量因子面板
 *
 * 功能：
 * - 展示 12-1 月动量（核心信号）+ 1m/3m/6m 收益率
 * - 52 周高低位置仪表（动量质量指标）
 * - Grade badge + 解读文本
 *
 * Phase F.14 — #0E1014 / #151619 / #00C087 dark-theme tokens
 * 学术来源：Jegadeesh & Titman (1993, 2001)
 */
import React, { useState } from "react";
import {
  type MomentumGrade,
  type MomentumSignalData,
  fetchMomentumSignal,
} from "../api/client";

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function gradeColor(grade: MomentumGrade): string {
  if (grade === "strong_momentum") return "#00C087";
  if (grade === "momentum") return "#34d399";
  if (grade === "neutral") return "#64748b";
  if (grade === "reversal_risk") return "#f59e0b";
  return "#ef4444"; // strong_reversal
}

function gradeLabel(grade: MomentumGrade): string {
  const labels: Record<MomentumGrade, string> = {
    strong_momentum: "强动量 ✓✓",
    momentum: "动量 ✓",
    neutral: "中性",
    reversal_risk: "反转风险 ⚠",
    strong_reversal: "强反转 ⚠⚠",
  };
  return labels[grade];
}

function retColor(pct: number): string {
  if (pct > 10) return "#00C087";
  if (pct > 0) return "#34d399";
  if (pct < -10) return "#ef4444";
  if (pct < 0) return "#f59e0b";
  return "#64748b";
}

function fmtPct(n: number): string {
  return n >= 0 ? `+${n.toFixed(1)}%` : `${n.toFixed(1)}%`;
}

// ---------------------------------------------------------------------------
// 子组件：收益率单格
// ---------------------------------------------------------------------------

function ReturnCell({ label, pct }: { label: string; pct: number }) {
  const color = retColor(pct);
  return (
    <div
      style={{
        background: "#0E1014",
        border: "1px solid #2a2d35",
        borderRadius: 5,
        padding: "6px 8px",
        textAlign: "center",
        flex: 1,
      }}
    >
      <div style={{ fontSize: 14, fontWeight: 700, color }}>
        {fmtPct(pct)}
      </div>
      <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>{label}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 子组件：52 周位置仪表
// ---------------------------------------------------------------------------

function PositionGauge({ data }: { data: MomentumSignalData }) {
  const pct = data.proximity_52w_high * 100;
  const color =
    pct >= 90 ? "#00C087" : pct >= 70 ? "#34d399" : pct >= 40 ? "#f59e0b" : "#ef4444";

  const rangeWidth = data.high_52w - data.low_52w;
  const fillPct =
    rangeWidth > 0
      ? ((data.current_price - data.low_52w) / rangeWidth) * 100
      : 50;
  const clampedFill = Math.max(0, Math.min(100, fillPct));

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
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 8,
        }}
      >
        <div style={{ fontSize: 11, fontWeight: 700, color: "#94a3b8", textTransform: "uppercase", letterSpacing: 0.5 }}>
          52 周区间位置
        </div>
        <span style={{ fontSize: 13, fontWeight: 700, color }}>
          {pct.toFixed(0)}% of 52w high
        </span>
      </div>

      {/* Range bar */}
      <div
        style={{
          position: "relative",
          height: 8,
          background: "#2a2d35",
          borderRadius: 4,
          marginBottom: 4,
        }}
      >
        <div
          style={{
            width: `${clampedFill}%`,
            height: "100%",
            background: `linear-gradient(90deg, #ef4444, ${color})`,
            borderRadius: 4,
            transition: "width 0.3s ease",
          }}
        />
      </div>

      {/* Labels */}
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 9, color: "#475569" }}>
        <span>52w Low ${data.low_52w.toFixed(2)}</span>
        <span style={{ color: "#e2e8f0", fontWeight: 700 }}>
          Now ${data.current_price.toFixed(2)}
        </span>
        <span>52w High ${data.high_52w.toFixed(2)}</span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export default function MomentumPanel() {
  const [inputTicker, setInputTicker] = useState("AAPL");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<MomentumSignalData | null>(null);

  async function handleQuery() {
    if (!inputTicker.trim()) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const data = await fetchMomentumSignal(inputTicker.trim().toUpperCase());
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
        <span>价格动量因子（Jegadeesh-Titman）</span>
        <span style={{ fontSize: 10, color: "#475569", fontWeight: 400 }}>
          来源：yfinance · 参考：JT 1993
        </span>
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
        <div>
          {/* 核心：12-1月动量 + grade */}
          <div
            style={{
              background: "#151619",
              border: "1px solid #2a2d35",
              borderRadius: 8,
              padding: "10px 14px",
              marginBottom: 10,
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
            }}
          >
            <div>
              <div style={{ fontSize: 10, color: "#64748b", marginBottom: 2 }}>
                12-1 月动量（Jegadeesh-Titman 核心因子）
              </div>
              <span
                style={{
                  fontSize: 32,
                  fontWeight: 700,
                  color: gradeColor(result.grade),
                }}
              >
                {fmtPct(result.momentum_12_1)}
              </span>
              <span
                style={{
                  marginLeft: 12,
                  fontSize: 12,
                  padding: "2px 10px",
                  borderRadius: 4,
                  background: gradeColor(result.grade) + "22",
                  color: gradeColor(result.grade),
                  fontWeight: 700,
                }}
              >
                {gradeLabel(result.grade)}
              </span>
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
              lineHeight: 1.5,
              marginBottom: 10,
            }}
          >
            {result.interpretation}
          </div>

          {/* 多期收益率 */}
          <div
            style={{
              display: "flex",
              gap: 6,
              marginBottom: 10,
            }}
          >
            <ReturnCell label="1 个月" pct={result.return_1m} />
            <ReturnCell label="3 个月" pct={result.return_3m} />
            <ReturnCell label="6 个月" pct={result.return_6m} />
            <ReturnCell label="12-1月动量" pct={result.momentum_12_1} />
          </div>

          {/* 52 周位置 */}
          <PositionGauge data={result} />

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
            <div>12-1月动量 = 过去 12 个月 skip 最近 1 个月的累计收益（规避短期反转）</div>
            <div>Jegadeesh & Titman (1993)：多空组合月均超额约 1%，在 40+ 国家验证有效</div>
          </div>

          <div style={{ marginTop: 8, fontSize: 10, color: "#475569" }}>
            数据日期：{result.as_of_date}　│　来源：yfinance　│　不构成投资建议
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
          输入股票代码查询 Jegadeesh-Titman 12-1 月动量因子
        </div>
      )}
    </div>
  );
}

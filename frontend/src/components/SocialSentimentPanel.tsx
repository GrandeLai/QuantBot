/**
 * SocialSentimentPanel — StockTwits 散户情绪 & Pump Risk 面板
 *
 * 功能：
 * - 看涨/看跌比例条（Bullish/Bearish ratio bar）
 * - Pump Risk 等级指示器（high / elevated / low）
 * - API 不可达时降级提示（api_accessible=false）
 *
 * Phase F.15 — 反向因子：散户聚集极值 → pump risk 信号
 * 学术来源：Baker & Wurgler (2006), Barber & Odean (2008)
 */
import React, { useState } from "react";
import {
  type PumpRiskLevel,
  type SentimentGrade,
  type SocialSentimentData,
  fetchSocialSentiment,
} from "../api/client";

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function gradeColor(grade: SentimentGrade): string {
  if (grade === "very_bullish") return "#00C087";
  if (grade === "bullish") return "#34d399";
  if (grade === "neutral") return "#64748b";
  if (grade === "bearish") return "#f59e0b";
  return "#ef4444"; // very_bearish
}

function gradeLabel(grade: SentimentGrade): string {
  const labels: Record<SentimentGrade, string> = {
    very_bullish: "极度看多 ⚠",
    bullish: "偏多",
    neutral: "中性",
    bearish: "偏空",
    very_bearish: "极度看空 ↑",
  };
  return labels[grade];
}

function riskColor(level: PumpRiskLevel): string {
  if (level === "high") return "#ef4444";
  if (level === "elevated") return "#f59e0b";
  return "#00C087";
}

function riskLabel(level: PumpRiskLevel): string {
  if (level === "high") return "高 ⚠⚠";
  if (level === "elevated") return "中等 ⚠";
  return "低";
}

// ---------------------------------------------------------------------------
// 子组件：Bullish/Bearish 比例条
// ---------------------------------------------------------------------------

function SentimentBar({ data }: { data: SocialSentimentData }) {
  const labeled = data.bullish_count + data.bearish_count;
  const bullishPct = labeled > 0 ? (data.bullish_count / labeled) * 100 : 50;
  const bearishPct = 100 - bullishPct;
  const grade = data.sentiment_grade;

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
        <div
          style={{
            fontSize: 11,
            fontWeight: 700,
            color: "#94a3b8",
            textTransform: "uppercase",
            letterSpacing: 0.5,
          }}
        >
          散户情绪比例
        </div>
        <span
          style={{
            fontSize: 12,
            fontWeight: 700,
            padding: "2px 8px",
            borderRadius: 4,
            background: gradeColor(grade) + "22",
            color: gradeColor(grade),
          }}
        >
          {gradeLabel(grade)}
        </span>
      </div>

      {/* Ratio bar */}
      <div
        style={{
          display: "flex",
          height: 10,
          borderRadius: 5,
          overflow: "hidden",
          marginBottom: 6,
        }}
      >
        <div
          style={{
            width: `${bullishPct}%`,
            background: "#00C087",
            transition: "width 0.4s ease",
          }}
        />
        <div
          style={{
            width: `${bearishPct}%`,
            background: "#ef4444",
            transition: "width 0.4s ease",
          }}
        />
      </div>

      {/* Labels */}
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: "#94a3b8" }}>
        <span style={{ color: "#00C087", fontWeight: 700 }}>
          看涨 {data.bullish_count} ({bullishPct.toFixed(0)}%)
        </span>
        <span style={{ color: "#64748b" }}>
          未标注 {data.total_messages - labeled}
        </span>
        <span style={{ color: "#ef4444", fontWeight: 700 }}>
          看跌 {data.bearish_count} ({bearishPct.toFixed(0)}%)
        </span>
      </div>

      <div style={{ marginTop: 4, fontSize: 9, color: "#475569" }}>
        共 {data.total_messages} 条消息
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 子组件：Pump Risk 指示器
// ---------------------------------------------------------------------------

function PumpRiskIndicator({ data }: { data: SocialSentimentData }) {
  const level = data.pump_risk_level;
  const color = riskColor(level);
  const scorePct = Math.round(data.pump_risk_score * 100);

  return (
    <div
      style={{
        background: "#151619",
        border: `1px solid ${color}44`,
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
        <div
          style={{
            fontSize: 11,
            fontWeight: 700,
            color: "#94a3b8",
            textTransform: "uppercase",
            letterSpacing: 0.5,
          }}
        >
          Pump Risk 等级
        </div>
        <span
          style={{
            fontSize: 13,
            fontWeight: 700,
            color,
          }}
        >
          {riskLabel(level)}
        </span>
      </div>

      {/* Score bar */}
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
            width: `${scorePct}%`,
            height: "100%",
            background: `linear-gradient(90deg, #00C087, ${color})`,
            borderRadius: 4,
            transition: "width 0.4s ease",
          }}
        />
        {/* Thresholds */}
        <div
          style={{
            position: "absolute",
            left: "30%",
            top: -2,
            bottom: -2,
            width: 1,
            background: "#475569",
          }}
        />
        <div
          style={{
            position: "absolute",
            left: "60%",
            top: -2,
            bottom: -2,
            width: 1,
            background: "#475569",
          }}
        />
      </div>

      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          fontSize: 9,
          color: "#475569",
        }}
      >
        <span>低</span>
        <span>中等</span>
        <span>高</span>
      </div>

      <div style={{ marginTop: 4, fontSize: 9, color: "#475569" }}>
        得分 {data.pump_risk_score.toFixed(4)}（阈值：{'>'} 0.6=高，{'>'} 0.3=中）
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export default function SocialSentimentPanel() {
  const [inputTicker, setInputTicker] = useState("AAPL");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<SocialSentimentData | null>(null);

  async function handleQuery() {
    if (!inputTicker.trim()) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const data = await fetchSocialSentiment(inputTicker.trim().toUpperCase());
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
        <span>散户情绪 & Pump Risk（StockTwits）</span>
        <span style={{ fontSize: 10, color: "#475569", fontWeight: 400 }}>
          来源：StockTwits · 参考：Baker &amp; Wurgler 2006
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
          {/* API 不可达降级提示 */}
          {!result.api_accessible && (
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
              ⚠ StockTwits API 暂时不可访问，情绪数据不可用。以下为降级默认值。
            </div>
          )}

          {/* 情绪比例条 */}
          <SentimentBar data={result} />

          {/* Pump Risk */}
          <PumpRiskIndicator data={result} />

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
            <div>Pump Risk = 0.7 × 看涨极值 + 0.3 × 消息量因子（饱和于 20 条）</div>
            <div>极度看多是<b style={{ color: "#f59e0b" }}>反向信号</b>——散户聚集顶部，机构通常在出货（Baker &amp; Wurgler 2006）</div>
          </div>

          <div style={{ marginTop: 8, fontSize: 10, color: "#475569" }}>
            数据日期：{result.as_of_date}　│　来源：StockTwits Public API　│　不构成投资建议
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
          输入股票代码查询 StockTwits 散户情绪与 Pump Risk 信号
        </div>
      )}
    </div>
  );
}

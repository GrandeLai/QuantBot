/**
 * AnalystConsensusPanel — 华尔街分析师共识 & 目标价面板
 *
 * 功能：
 * - 分析师综合评级 badge（强烈买入 → 强烈卖出）
 * - 均值评分仪表 + 覆盖分析师数量
 * - 目标价 高/中/低 + 隐含涨跌幅
 * - 数据不可用降级提示
 *
 * Phase F.19 — 分析师共识因子
 * 来源：yfinance recommendationMean (1=Strong Buy, 5=Strong Sell)
 */
import React, { useState } from "react";
import {
  type AnalystConsensusData,
  type AnalystGrade,
  fetchAnalystConsensus,
} from "../api/client";

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function gradeColor(grade: AnalystGrade): string {
  if (grade === "strong_buy") return "#00C087";
  if (grade === "buy") return "#34d399";
  if (grade === "hold") return "#f59e0b";
  if (grade === "sell") return "#f87171";
  if (grade === "strong_sell") return "#ef4444";
  return "#64748b"; // no_coverage
}

function gradeLabel(grade: AnalystGrade): string {
  const labels: Record<AnalystGrade, string> = {
    strong_buy: "强烈买入 ✓✓",
    buy: "买入 ✓",
    hold: "持有",
    sell: "卖出 ✗",
    strong_sell: "强烈卖出 ✗✗",
    no_coverage: "无覆盖",
  };
  return labels[grade];
}

function fmtPct(n: number): string {
  return n >= 0 ? `+${n.toFixed(1)}%` : `${n.toFixed(1)}%`;
}

function upsideColor(pct: number | null): string {
  if (pct === null) return "#64748b";
  if (pct > 15) return "#00C087";
  if (pct > 0) return "#34d399";
  if (pct < -10) return "#ef4444";
  if (pct < 0) return "#f59e0b";
  return "#64748b";
}

// ---------------------------------------------------------------------------
// 子组件：评分仪表（1-5 scale）
// ---------------------------------------------------------------------------

function RecommendationMeter({ mean }: { mean: number }) {
  // Map 1-5 to 0-100% for bar display (inverted: 1 = best = full green)
  const fillPct = ((5 - mean) / 4) * 100;
  const color = gradeColor(
    mean <= 1.5
      ? "strong_buy"
      : mean <= 2.5
      ? "buy"
      : mean <= 3.5
      ? "hold"
      : mean <= 4.5
      ? "sell"
      : "strong_sell"
  );

  return (
    <div style={{ marginBottom: 8 }}>
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
            width: `${fillPct}%`,
            height: "100%",
            background: `linear-gradient(90deg, #ef4444, ${color})`,
            borderRadius: 4,
            transition: "width 0.4s ease",
          }}
        />
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 9, color: "#475569" }}>
        <span>卖出</span>
        <span>持有</span>
        <span>买入</span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export default function AnalystConsensusPanel() {
  const [inputTicker, setInputTicker] = useState("AAPL");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<AnalystConsensusData | null>(null);

  async function handleQuery() {
    if (!inputTicker.trim()) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const data = await fetchAnalystConsensus(inputTicker.trim().toUpperCase());
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
        <span>分析师共识 & 目标价</span>
        <span style={{ fontSize: 10, color: "#475569", fontWeight: 400 }}>
          来源：yfinance · Wall Street 分析师评级
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
          {/* 降级提示 */}
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
              ⚠ 分析师评级数据不可用（该股票可能无分析师覆盖或 yfinance 暂时不可用）。
            </div>
          )}

          {/* Grade + 评分仪表 */}
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
              <span
                style={{
                  fontSize: 18,
                  fontWeight: 700,
                  color: gradeColor(result.grade),
                  padding: "2px 10px",
                  borderRadius: 4,
                  background: gradeColor(result.grade) + "22",
                }}
              >
                {gradeLabel(result.grade)}
              </span>
              <div style={{ textAlign: "right" }}>
                {result.recommendation_mean !== null && (
                  <span style={{ fontSize: 14, fontWeight: 700, color: "#e2e8f0" }}>
                    {result.recommendation_mean.toFixed(2)} / 5
                  </span>
                )}
                <div style={{ fontSize: 10, color: "#475569" }}>
                  {result.num_analysts} 位分析师
                </div>
              </div>
            </div>

            {result.recommendation_mean !== null && (
              <RecommendationMeter mean={result.recommendation_mean} />
            )}
          </div>

          {/* 目标价 */}
          {(result.target_mean_price !== null || result.upside_pct !== null) && (
            <div
              style={{
                display: "flex",
                gap: 6,
                marginBottom: 10,
              }}
            >
              {result.current_price !== null && (
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
                  <div style={{ fontSize: 13, fontWeight: 700, color: "#e2e8f0" }}>
                    ${result.current_price.toFixed(2)}
                  </div>
                  <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>当前价</div>
                </div>
              )}
              {result.target_low_price !== null && (
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
                  <div style={{ fontSize: 13, fontWeight: 700, color: "#f59e0b" }}>
                    ${result.target_low_price.toFixed(2)}
                  </div>
                  <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>目标低</div>
                </div>
              )}
              {result.target_mean_price !== null && (
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
                  <div style={{ fontSize: 13, fontWeight: 700, color: "#e2e8f0" }}>
                    ${result.target_mean_price.toFixed(2)}
                  </div>
                  <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>目标均值</div>
                </div>
              )}
              {result.target_high_price !== null && (
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
                  <div style={{ fontSize: 13, fontWeight: 700, color: "#00C087" }}>
                    ${result.target_high_price.toFixed(2)}
                  </div>
                  <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>目标高</div>
                </div>
              )}
              {result.upside_pct !== null && (
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
                  <div
                    style={{
                      fontSize: 16,
                      fontWeight: 700,
                      color: upsideColor(result.upside_pct),
                    }}
                  >
                    {fmtPct(result.upside_pct)}
                  </div>
                  <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>隐含涨跌</div>
                </div>
              )}
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
            <div>评分均值 1=强烈买入 → 5=强烈卖出；数据来自 yfinance（延迟 T+1）</div>
            <div>分析师目标价通常有 12 个月时间维度；与 DCF/Fundamental 信号联合验证更可靠</div>
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
          输入股票代码查询华尔街分析师共识评级与目标价
        </div>
      )}
    </div>
  );
}

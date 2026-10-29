/**
 * EarningsQualityPanel — 盈利质量三件套面板
 *
 * 功能：
 * - Piotroski F-Score (0-9) 条形图 + 9 个子组件 Pass/Fail
 * - Beneish M-Score 操纵风险指示器（阈值 -2.22）
 * - Sloan 应计比率质量评分
 * - 综合盈利质量等级（high/average/low/manipulator_risk）
 *
 * Phase F.20 — 盈利质量三件套
 * 来源：yfinance 年度财报（income statement / balance sheet / cash flow）
 */
import { useState } from "react";
import {
  type EarningsQualityData,
  type EarningsQualityGrade,
  type FScoreGrade,
  fetchEarningsQuality,
} from "../api/client";

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function gradeColor(grade: EarningsQualityGrade): string {
  if (grade === "high_quality") return "#00C087";
  if (grade === "average_quality") return "#f59e0b";
  if (grade === "low_quality") return "#f87171";
  if (grade === "manipulator_risk") return "#ef4444";
  return "#64748b";
}

function gradeLabel(grade: EarningsQualityGrade): string {
  const labels: Record<EarningsQualityGrade, string> = {
    high_quality: "盈利质量高 ✓✓",
    average_quality: "质量一般",
    low_quality: "质量偏低 ✗",
    manipulator_risk: "⚠ 操纵风险",
  };
  return labels[grade];
}

function fScoreGradeLabel(grade: FScoreGrade): string {
  const labels: Record<FScoreGrade, string> = {
    very_strong: "极强",
    strong: "较强",
    average: "一般",
    weak: "偏弱",
  };
  return labels[grade];
}

function fScoreGradeColor(grade: FScoreGrade): string {
  if (grade === "very_strong") return "#00C087";
  if (grade === "strong") return "#34d399";
  if (grade === "average") return "#f59e0b";
  return "#f87171";
}

// ---------------------------------------------------------------------------
// 子组件：F-Score 条形
// ---------------------------------------------------------------------------

function FScoreBar({ score }: { score: number }) {
  const pct = (score / 9) * 100;
  const color =
    score >= 8 ? "#00C087" : score >= 6 ? "#34d399" : score >= 3 ? "#f59e0b" : "#f87171";
  return (
    <div style={{ marginBottom: 6 }}>
      <div style={{ position: "relative", height: 10, background: "#2a2d35", borderRadius: 5 }}>
        <div
          style={{
            width: `${pct}%`,
            height: "100%",
            background: `linear-gradient(90deg, #475569, ${color})`,
            borderRadius: 5,
            transition: "width 0.4s ease",
          }}
        />
      </div>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          fontSize: 9,
          color: "#475569",
          marginTop: 2,
        }}
      >
        <span>0</span>
        <span>3</span>
        <span>6</span>
        <span>9</span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 子组件：M-Score 指示器
// ---------------------------------------------------------------------------

function MScoreIndicator({ mScore }: { mScore: number }) {
  // M ranges from roughly -10 to +5; normalize -6..+2 for display
  const MIN = -6;
  const MAX = 2;
  const clipped = Math.max(MIN, Math.min(MAX, mScore));
  const pct = ((clipped - MIN) / (MAX - MIN)) * 100;
  // threshold -2.22 as % of display range
  const threshPct = ((-2.22 - MIN) / (MAX - MIN)) * 100;
  const isRisk = mScore > -2.22;

  return (
    <div style={{ marginBottom: 6 }}>
      <div
        style={{
          position: "relative",
          height: 10,
          background: "#2a2d35",
          borderRadius: 5,
        }}
      >
        {/* Safe zone (green) */}
        <div
          style={{
            position: "absolute",
            left: 0,
            width: `${threshPct}%`,
            height: "100%",
            background: "#1e3a2f",
            borderRadius: "5px 0 0 5px",
          }}
        />
        {/* Risk zone (red) */}
        <div
          style={{
            position: "absolute",
            left: `${threshPct}%`,
            right: 0,
            height: "100%",
            background: "#3a1e1e",
            borderRadius: "0 5px 5px 0",
          }}
        />
        {/* Current position marker */}
        <div
          style={{
            position: "absolute",
            left: `${pct}%`,
            top: -2,
            width: 3,
            height: 14,
            background: isRisk ? "#ef4444" : "#00C087",
            borderRadius: 2,
            transform: "translateX(-50%)",
          }}
        />
        {/* Threshold line */}
        <div
          style={{
            position: "absolute",
            left: `${threshPct}%`,
            top: 0,
            width: 1,
            height: "100%",
            background: "#f59e0b",
          }}
        />
      </div>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          fontSize: 9,
          color: "#475569",
          marginTop: 2,
        }}
      >
        <span>-6 (安全)</span>
        <span style={{ color: "#f59e0b" }}>-2.22 阈值</span>
        <span>+2 (风险)</span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 子组件：F-Score 分项列表
// ---------------------------------------------------------------------------

function FScoreComponents({ components }: { components: Record<string, boolean> }) {
  const entries = Object.entries(components);
  if (entries.length === 0) return null;

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "1fr 1fr",
        gap: "3px 8px",
        marginTop: 8,
      }}
    >
      {entries.map(([name, pass]) => (
        <div
          key={name}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 5,
            fontSize: 10,
            color: pass ? "#34d399" : "#94a3b8",
          }}
        >
          <span style={{ color: pass ? "#00C087" : "#ef4444", fontWeight: 700 }}>
            {pass ? "✓" : "✗"}
          </span>
          <span>{name}</span>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export default function EarningsQualityPanel() {
  const [inputTicker, setInputTicker] = useState("AAPL");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<EarningsQualityData | null>(null);

  async function handleQuery() {
    if (!inputTicker.trim()) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const data = await fetchEarningsQuality(inputTicker.trim().toUpperCase());
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

  const cardStyle: React.CSSProperties = {
    background: "#151619",
    border: "1px solid #2a2d35",
    borderRadius: 8,
    padding: "10px 14px",
    marginBottom: 10,
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
        <span>盈利质量三件套</span>
        <span style={{ fontSize: 10, color: "#475569", fontWeight: 400 }}>
          Piotroski F-Score · Beneish M-Score · Sloan Accrual
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
              ⚠ 财报数据不可用（yfinance 暂时无法获取该股票年度财务报表）。
            </div>
          )}

          {/* 综合评级 */}
          <div style={cardStyle}>
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
                  fontSize: 16,
                  fontWeight: 700,
                  color: gradeColor(result.quality_grade),
                  padding: "2px 10px",
                  borderRadius: 4,
                  background: gradeColor(result.quality_grade) + "22",
                }}
              >
                {gradeLabel(result.quality_grade)}
              </span>
              <span style={{ fontSize: 10, color: "#475569" }}>综合盈利质量</span>
            </div>
          </div>

          {/* Piotroski F-Score */}
          {result.f_score !== null && (
            <div style={cardStyle}>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  marginBottom: 6,
                }}
              >
                <span style={{ fontSize: 12, color: "#94a3b8" }}>
                  Piotroski F-Score
                </span>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  {result.f_score_grade && (
                    <span
                      style={{
                        fontSize: 10,
                        color: fScoreGradeColor(result.f_score_grade),
                        padding: "1px 6px",
                        borderRadius: 3,
                        background: fScoreGradeColor(result.f_score_grade) + "22",
                      }}
                    >
                      {fScoreGradeLabel(result.f_score_grade)}
                    </span>
                  )}
                  <span style={{ fontSize: 18, fontWeight: 700, color: "#e2e8f0" }}>
                    {result.f_score}/9
                  </span>
                </div>
              </div>
              <FScoreBar score={result.f_score} />
              <FScoreComponents components={result.f_score_components} />
            </div>
          )}

          {/* Beneish M-Score */}
          {result.m_score !== null && (
            <div style={cardStyle}>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  marginBottom: 6,
                }}
              >
                <span style={{ fontSize: 12, color: "#94a3b8" }}>Beneish M-Score</span>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span
                    style={{
                      fontSize: 10,
                      color: result.manipulation_risk ? "#ef4444" : "#00C087",
                      padding: "1px 6px",
                      borderRadius: 3,
                      background: result.manipulation_risk ? "#ef444422" : "#00C08722",
                    }}
                  >
                    {result.manipulation_risk ? "⚠ 操纵风险" : "无操纵风险"}
                  </span>
                  <span
                    style={{
                      fontSize: 16,
                      fontWeight: 700,
                      color: result.manipulation_risk ? "#ef4444" : "#e2e8f0",
                    }}
                  >
                    {result.m_score.toFixed(2)}
                  </span>
                </div>
              </div>
              <MScoreIndicator mScore={result.m_score} />
            </div>
          )}

          {/* Sloan Accrual */}
          {result.accrual_ratio !== null && (
            <div style={cardStyle}>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                }}
              >
                <span style={{ fontSize: 12, color: "#94a3b8" }}>Sloan 应计比率</span>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  {result.accrual_quality && (
                    <span
                      style={{
                        fontSize: 10,
                        color:
                          result.accrual_quality === "high"
                            ? "#00C087"
                            : result.accrual_quality === "medium"
                            ? "#f59e0b"
                            : "#ef4444",
                        padding: "1px 6px",
                        borderRadius: 3,
                        background:
                          result.accrual_quality === "high"
                            ? "#00C08722"
                            : result.accrual_quality === "medium"
                            ? "#f59e0b22"
                            : "#ef444422",
                      }}
                    >
                      {result.accrual_quality === "high"
                        ? "高质量"
                        : result.accrual_quality === "medium"
                        ? "中等"
                        : "低质量"}
                    </span>
                  )}
                  <span
                    style={{
                      fontSize: 16,
                      fontWeight: 700,
                      color:
                        Math.abs(result.accrual_ratio) > 0.1
                          ? "#ef4444"
                          : "#e2e8f0",
                    }}
                  >
                    {(result.accrual_ratio * 100).toFixed(1)}%
                  </span>
                </div>
              </div>
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
            <div>F-Score 0-2=偏弱 / 3-5=一般 / 6-7=较强 / 8-9=极强（Piotroski 2000）</div>
            <div>M-Score 阈值 -2.22：上方警示财务操纵可能（Beneish 1999）</div>
            <div>应计比率 &gt;10% 表明盈利中非现金成分过高（Sloan 1996）</div>
          </div>

          <div style={{ marginTop: 8, fontSize: 10, color: "#475569" }}>
            数据日期：{result.as_of_date}　│　来源：yfinance 年度财报　│　不构成投资建议
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
          输入股票代码查询 Piotroski F-Score / Beneish M-Score / Sloan 应计比率
        </div>
      )}
    </div>
  );
}

/**
 * DCFPanel — DCF + Monte Carlo 估值面板
 *
 * 功能：
 * - 展示 P5/P50/P95 公允价值区间
 * - WACC 组成（权益成本 / 债务成本 / 杠杆权重）
 * - 安全边际 + 估值标签（deep_value → overheated）
 * - 预测 FCF 序列条形图
 *
 * Phase F.8 — #0E1014 / #151619 / #00C087 dark-theme tokens
 */
import { useState } from "react";
import {
  type DCFResultData,
  type WACCData,
  fetchDCFValuation,
} from "../api/client";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

type ValuationLabel = DCFResultData["valuation"];

function valuationColor(v: ValuationLabel): string {
  if (v === "deep_value") return "#00C087";
  if (v === "undervalued") return "#34d399";
  if (v === "fair") return "#64748b";
  if (v === "overvalued") return "#f59e0b";
  return "#ef4444"; // overheated
}

function valuationCN(v: ValuationLabel): string {
  const map: Record<ValuationLabel, string> = {
    deep_value: "深度价值",
    undervalued: "低估",
    fair: "合理",
    overvalued: "高估",
    overheated: "极度高估",
  };
  return map[v] ?? v;
}

function fmt(n: number, digits = 2): string {
  return n.toFixed(digits);
}

function fmtPct(n: number): string {
  return `${(n * 100).toFixed(1)}%`;
}

function fmtB(n: number): string {
  if (Math.abs(n) >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (Math.abs(n) >= 1e6) return `$${(n / 1e6).toFixed(1)}M`;
  return `$${n.toFixed(0)}`;
}

// ---------------------------------------------------------------------------
// WACC breakdown sub-component
// ---------------------------------------------------------------------------

function WACCCard({ w }: { w: WACCData }) {
  const rows: [string, string][] = [
    ["WACC（加权平均资本成本）", fmtPct(w.wacc)],
    ["权益成本（Ke）", fmtPct(w.cost_of_equity)],
    ["税后债务成本（Kd×(1-T)）", fmtPct(w.cost_of_debt * (1 - w.tax_rate))],
    ["Beta（β）", fmt(w.beta)],
    ["无风险利率（Rf）", fmtPct(w.risk_free_rate)],
    ["税率", fmtPct(w.tax_rate)],
    ["债务权重", fmtPct(w.debt_weight)],
    ["权益权重", fmtPct(w.equity_weight)],
  ];

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
        WACC 分解
      </div>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: "6px 12px",
        }}
      >
        {rows.map(([label, val]) => (
          <div
            key={label}
            style={{
              background: "#0E1014",
              border: "1px solid #2a2d35",
              borderRadius: 5,
              padding: "6px 10px",
            }}
          >
            <div style={{ fontSize: 10, color: "#64748b" }}>{label}</div>
            <div style={{ fontSize: 13, fontWeight: 600, color: "#e2e8f0" }}>
              {val}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Projected FCF bar chart
// ---------------------------------------------------------------------------

function FCFBars({ fcfs }: { fcfs: number[] }) {
  if (!fcfs.length) return null;
  const max = Math.max(...fcfs);
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
        预测自由现金流（FCF）
      </div>
      <div
        style={{
          display: "flex",
          gap: 6,
          alignItems: "flex-end",
          height: 64,
        }}
      >
        {fcfs.map((v, i) => {
          const pct = max > 0 ? (v / max) * 100 : 0;
          return (
            <div
              key={i}
              style={{
                flex: 1,
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                gap: 4,
                height: "100%",
                justifyContent: "flex-end",
              }}
            >
              <div
                style={{ fontSize: 9, color: "#64748b" }}
              >
                {fmtB(v)}
              </div>
              <div
                style={{
                  width: "100%",
                  height: `${Math.max(4, pct)}%`,
                  background: "#00C087",
                  borderRadius: "2px 2px 0 0",
                  opacity: 0.7 + 0.3 * (i / Math.max(1, fcfs.length - 1)),
                }}
              />
              <div style={{ fontSize: 9, color: "#64748b" }}>
                Y{i + 1}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function DCFPanel() {
  const [inputTicker, setInputTicker] = useState("AAPL");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<DCFResultData | null>(null);

  async function handleQuery() {
    const t = inputTicker.trim().toUpperCase();
    if (!t) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const data = await fetchDCFValuation(t);
      setResult(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "查询失败");
    } finally {
      setLoading(false);
    }
  }

  const vc = result ? valuationColor(result.valuation) : "#64748b";
  const vl = result ? valuationCN(result.valuation) : "";
  const mosPct = result
    ? `${result.margin_of_safety >= 0 ? "+" : ""}${(result.margin_of_safety * 100).toFixed(1)}%`
    : "";

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
        <span>DCF + 蒙特卡洛估值</span>
        {result && (
          <span
            style={{
              fontSize: 11,
              padding: "2px 8px",
              borderRadius: 4,
              background: vc + "22",
              color: vc,
              fontWeight: 700,
            }}
          >
            {vl}　安全边际 {mosPct}
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
          {loading ? "计算中..." : "估值"}
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
          {/* 公允价值区间 */}
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
              蒙特卡洛公允价值区间（1000 次模拟）
            </div>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "1fr 1fr 1fr",
                gap: 8,
                marginBottom: 12,
              }}
            >
              {(
                [
                  ["P5（悲观）", result.fair_value_p5, "#f59e0b"],
                  ["P50（中性）", result.fair_value_p50, "#00C087"],
                  ["P95（乐观）", result.fair_value_p95, "#818cf8"],
                ] as [string, number, string][]
              ).map(([label, val, color]) => (
                <div
                  key={label}
                  style={{
                    background: "#0E1014",
                    border: `1px solid ${color}44`,
                    borderRadius: 6,
                    padding: "8px 12px",
                    textAlign: "center",
                  }}
                >
                  <div style={{ fontSize: 10, color: "#64748b", marginBottom: 3 }}>
                    {label}
                  </div>
                  <div style={{ fontSize: 18, fontWeight: 700, color }}>
                    ${fmt(val)}
                  </div>
                </div>
              ))}
            </div>

            {/* 现价 vs P50 进度条 */}
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
                <span>现价 ${fmt(result.current_price)}</span>
                <span style={{ color: vc }}>
                  vs P50 ${fmt(result.fair_value_p50)}　{mosPct}
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
                    width: `${Math.min(100, (result.current_price / result.fair_value_p50) * 100)}%`,
                    height: "100%",
                    background: vc,
                    borderRadius: 3,
                    transition: "width 0.3s ease",
                  }}
                />
              </div>
            </div>
          </div>

          {/* DCF 基础指标 */}
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
              DCF 基础指标
            </div>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "1fr 1fr 1fr",
                gap: 8,
              }}
            >
              {[
                ["基准 FCF", fmtB(result.base_fcf)],
                ["NPV（FCF）", fmtB(result.npv_fcf)],
                ["终值（PV）", fmtB(result.terminal_value_pv)],
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
                  <div style={{ fontSize: 13, fontWeight: 600, color: "#e2e8f0" }}>
                    {val as string}
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* 预测 FCF */}
          <FCFBars fcfs={result.projected_fcfs} />

          {/* WACC */}
          <WACCCard w={result.wacc_components} />

          <div style={{ marginTop: 8, fontSize: 10, color: "#475569" }}>
            数据日期：{result.as_of_date}　│　来源：yfinance / CAPM　│　蒙特卡洛 1000 次　│　不构成投资建议
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
          输入股票代码后点击"估值"，获取 DCF + 蒙特卡洛公允价值区间
        </div>
      )}
    </div>
  );
}

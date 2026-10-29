/**
 * IVRankPanel — 期权隐含波动率排名与历史波动率面板
 *
 * 功能：
 * - IV Rank (0-100) 仪表盘 + 买/卖信号
 * - IV Percentile
 * - HV10 / HV20 / HV30 / HV60 历史波动率对比
 * - Put/Call Skew（市场偏向下行保护 vs 上行投机）
 * - 期权期限结构表（ATM IV by DTE）
 *
 * Phase F.24 — IV Rank & Volatility Monitor
 * 数据来源：yfinance 期权链 + 价格历史
 */
import { useState } from "react";
import {
  type IVRankData,
  type IVSignal,
  type TermStructurePoint,
  fetchIVRank,
} from "../api/client";

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function signalColor(signal: IVSignal): string {
  if (signal === "buy_options") return "#00C087";
  if (signal === "sell_options") return "#ef4444";
  if (signal === "neutral") return "#f59e0b";
  return "#475569";
}

function signalLabel(signal: IVSignal): string {
  if (signal === "buy_options") return "★ 买入期权（IV 偏低）";
  if (signal === "sell_options") return "✗ 卖出期权（IV 偏高）";
  if (signal === "neutral") return "中性";
  return "—";
}

function fmtPct(v: number | null, decimals = 1): string {
  if (v == null) return "—";
  return `${v.toFixed(decimals)}%`;
}

function fmtPp(v: number | null): string {
  if (v == null) return "—";
  const sign = v >= 0 ? "+" : "";
  return `${sign}${v.toFixed(1)}pp`;
}

// ---------------------------------------------------------------------------
// IV Rank Gauge — simple horizontal bar
// ---------------------------------------------------------------------------

function IVRankGauge({ rank }: { rank: number | null }) {
  if (rank == null) {
    return (
      <div style={{ color: "#475569", fontSize: 12, textAlign: "center", padding: "8px 0" }}>
        IV Rank 不可用
      </div>
    );
  }

  const color = rank < 20 ? "#00C087" : rank > 80 ? "#ef4444" : "#f59e0b";
  const pct = Math.max(0, Math.min(100, rank));

  return (
    <div style={{ margin: "4px 0 8px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
        <span style={{ fontSize: 10, color: "#00C087" }}>0 — 买入</span>
        <span style={{ fontSize: 13, fontWeight: 700, color }}>IV Rank {rank.toFixed(0)}</span>
        <span style={{ fontSize: 10, color: "#ef4444" }}>卖出 — 100</span>
      </div>
      <div
        style={{
          background: "#1a1d24",
          borderRadius: 4,
          height: 8,
          position: "relative",
          overflow: "hidden",
        }}
      >
        {/* green zone 0-20 */}
        <div
          style={{
            position: "absolute",
            left: 0,
            top: 0,
            bottom: 0,
            width: "20%",
            background: "#00C08722",
          }}
        />
        {/* red zone 80-100 */}
        <div
          style={{
            position: "absolute",
            right: 0,
            top: 0,
            bottom: 0,
            width: "20%",
            background: "#ef444422",
          }}
        />
        {/* fill */}
        <div
          style={{
            position: "absolute",
            left: 0,
            top: 0,
            bottom: 0,
            width: `${pct}%`,
            background: color,
            borderRadius: 4,
            transition: "width 0.4s ease",
          }}
        />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// HV Bars
// ---------------------------------------------------------------------------

function HVRow({ label, val }: { label: string; val: number | null }) {
  return (
    <div
      style={{
        display: "flex",
        justifyContent: "space-between",
        padding: "4px 0",
        borderBottom: "1px solid #1a1d24",
        fontSize: 11,
      }}
    >
      <span style={{ color: "#94a3b8" }}>{label}</span>
      <span style={{ color: val != null ? "#e2e8f0" : "#475569", fontWeight: 600 }}>
        {fmtPct(val)}
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Term Structure Table
// ---------------------------------------------------------------------------

function TermStructureTable({ pts }: { pts: TermStructurePoint[] }) {
  if (pts.length === 0) return null;

  return (
    <div
      style={{
        background: "#151619",
        border: "1px solid #2a2d35",
        borderRadius: 8,
        marginBottom: 10,
        overflow: "hidden",
      }}
    >
      <div
        style={{
          padding: "5px 10px",
          background: "#1a1d24",
          fontSize: 9,
          color: "#475569",
          fontWeight: 600,
          display: "flex",
          justifyContent: "space-between",
        }}
      >
        <span>到期日</span>
        <span>DTE</span>
        <span>ATM IV</span>
      </div>
      {pts.map((pt, i) => (
        <div
          key={i}
          style={{
            display: "flex",
            justifyContent: "space-between",
            padding: "5px 10px",
            borderBottom: i < pts.length - 1 ? "1px solid #1a1d24" : "none",
            fontSize: 11,
          }}
        >
          <span style={{ color: "#94a3b8" }}>{pt.expiry}</span>
          <span style={{ color: "#64748b" }}>{pt.days_to_expiry}d</span>
          <span style={{ color: "#e2e8f0", fontWeight: 600 }}>{fmtPct(pt.atm_iv)}</span>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export default function IVRankPanel() {
  const [inputTicker, setInputTicker] = useState("AAPL");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<IVRankData | null>(null);

  async function handleQuery() {
    if (!inputTicker.trim()) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const data = await fetchIVRank(inputTicker.trim().toUpperCase());
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
        <span>期权 IV Rank 波动率面板</span>
        <span style={{ fontSize: 10, color: "#475569", fontWeight: 400 }}>
          IV Rank · HV · Skew · 期限结构
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
              ⚠ 数据不可用（yfinance 暂时无法获取）。
            </div>
          )}

          {/* 信号徽章 */}
          {result.iv_signal !== "no_data" && (
            <div style={{ marginBottom: 12 }}>
              <span
                style={{
                  display: "inline-block",
                  background: signalColor(result.iv_signal) + "22",
                  border: `1px solid ${signalColor(result.iv_signal)}55`,
                  color: signalColor(result.iv_signal),
                  borderRadius: 5,
                  padding: "3px 10px",
                  fontSize: 12,
                  fontWeight: 700,
                }}
              >
                {signalLabel(result.iv_signal)}
              </span>
            </div>
          )}

          {/* IV Rank 仪表盘 */}
          <div
            style={{
              background: "#151619",
              border: "1px solid #2a2d35",
              borderRadius: 8,
              padding: "10px 14px",
              marginBottom: 10,
            }}
          >
            <IVRankGauge rank={result.iv_rank} />

            {/* IV 关键数字行 */}
            <div style={{ display: "flex", gap: 8, marginTop: 4 }}>
              <div
                style={{
                  flex: 1,
                  background: "#0E1014",
                  border: "1px solid #2a2d35",
                  borderRadius: 5,
                  padding: "6px 8px",
                  textAlign: "center",
                }}
              >
                <div style={{ fontSize: 14, fontWeight: 700, color: "#e2e8f0" }}>
                  {fmtPct(result.current_iv)}
                </div>
                <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>ATM IV</div>
              </div>
              <div
                style={{
                  flex: 1,
                  background: "#0E1014",
                  border: "1px solid #2a2d35",
                  borderRadius: 5,
                  padding: "6px 8px",
                  textAlign: "center",
                }}
              >
                <div style={{ fontSize: 14, fontWeight: 700, color: "#e2e8f0" }}>
                  {result.iv_percentile != null ? `${result.iv_percentile.toFixed(0)}%` : "—"}
                </div>
                <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>IV 百分位</div>
              </div>
              {result.put_call_skew != null && (
                <div
                  style={{
                    flex: 1,
                    background: "#0E1014",
                    border: "1px solid #2a2d35",
                    borderRadius: 5,
                    padding: "6px 8px",
                    textAlign: "center",
                  }}
                >
                  <div
                    style={{
                      fontSize: 14,
                      fontWeight: 700,
                      color: result.put_call_skew > 0 ? "#f59e0b" : "#34d399",
                    }}
                  >
                    {fmtPp(result.put_call_skew)}
                  </div>
                  <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>Put/Call Skew</div>
                </div>
              )}
            </div>
          </div>

          {/* 历史波动率 */}
          <div
            style={{
              background: "#151619",
              border: "1px solid #2a2d35",
              borderRadius: 8,
              padding: "10px 14px",
              marginBottom: 10,
            }}
          >
            <div style={{ fontSize: 10, color: "#475569", marginBottom: 6, fontWeight: 600 }}>
              历史波动率（年化）
            </div>
            <HVRow label="HV10" val={result.hv10} />
            <HVRow label="HV20" val={result.hv20} />
            <HVRow label="HV30" val={result.hv30} />
            <HVRow label="HV60" val={result.hv60} />
          </div>

          {/* 期限结构 */}
          {result.term_structure.length > 0 && (
            <TermStructureTable pts={result.term_structure} />
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
            <div>IV Rank &lt; 20 → 期权偏便宜，适合买入期权（做多波动率）</div>
            <div>IV Rank &gt; 80 → 期权偏贵，适合卖出期权策略（Iron Condor、Covered Call 等）</div>
            <div>IV 以 HV30 滚动序列作为历史代理（免费数据近似）</div>
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
          输入股票代码查询 IV Rank 与波动率指标
        </div>
      )}
    </div>
  );
}

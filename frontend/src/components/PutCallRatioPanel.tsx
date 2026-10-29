/**
 * PutCallRatioPanel — Put/Call Ratio 期权情绪面板
 *
 * 功能：
 * - 全链汇总 Put/Call Ratio（成交量 + 持仓量）
 * - 情绪分级与反向信号（极度悲观 = 反向偏多）
 * - 各到期日 PCR 明细
 * - Call/Put 总量对比
 *
 * Phase F.26 — Put/Call Ratio & Options Sentiment
 * 数据来源：yfinance 期权链
 */
import { useState } from "react";
import {
  type ExpiryPCR,
  type PCRData,
  type PCRSentiment,
  fetchPutCallRatio,
} from "../api/client";

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function sentimentColor(s: PCRSentiment): string {
  if (s === "extreme_bearish") return "#ef4444";
  if (s === "bearish") return "#f97316";
  if (s === "neutral") return "#94a3b8";
  if (s === "bullish") return "#34d399";
  if (s === "extreme_bullish") return "#00C087";
  return "#475569";
}

function sentimentLabel(s: PCRSentiment): string {
  const labels: Record<PCRSentiment, string> = {
    extreme_bearish: "🔴 极度恐慌（反向偏多）",
    bearish: "↓ 偏悲观",
    neutral: "→ 中性",
    bullish: "↑ 偏乐观",
    extreme_bullish: "🟢 极度贪婪（反向偏空）",
    unknown: "—",
  };
  return labels[s];
}

function fmtPCR(v: number | null): string {
  if (v == null) return "—";
  return v.toFixed(2);
}

function fmtVol(v: number): string {
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000) return `${(v / 1_000).toFixed(0)}K`;
  return `${v}`;
}

// ---------------------------------------------------------------------------
// PCR Gauge
// ---------------------------------------------------------------------------

function PCRGauge({ pcr }: { pcr: number | null }) {
  if (pcr == null) {
    return (
      <div style={{ color: "#475569", fontSize: 12, textAlign: "center" }}>
        PCR 不可用
      </div>
    );
  }

  // PCR range 0 → 2+ (cap at 2.0 for display)
  const display = Math.min(pcr, 2.0);
  const pct = (display / 2.0) * 100;

  const color =
    pcr >= 1.5 ? "#ef4444"
    : pcr >= 1.0 ? "#f97316"
    : pcr >= 0.7 ? "#94a3b8"
    : pcr >= 0.5 ? "#34d399"
    : "#00C087";

  return (
    <div style={{ margin: "4px 0 8px" }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          marginBottom: 4,
          fontSize: 10,
        }}
      >
        <span style={{ color: "#00C087" }}>0.0 极乐观</span>
        <span style={{ fontSize: 14, fontWeight: 700, color }}>PCR {pcr.toFixed(2)}</span>
        <span style={{ color: "#ef4444" }}>极恐慌 2.0+</span>
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
        {/* neutral zone 0.7-1.0 */}
        <div
          style={{
            position: "absolute",
            left: "35%",
            top: 0,
            bottom: 0,
            width: "15%",
            background: "#94a3b833",
          }}
        />
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
// Expiry breakdown table row
// ---------------------------------------------------------------------------

function ExpiryRow({ ep }: { ep: ExpiryPCR }) {
  const pcr = ep.volume_pcr;
  const color =
    pcr == null ? "#475569"
    : pcr >= 1.5 ? "#ef4444"
    : pcr >= 1.0 ? "#f97316"
    : pcr < 0.5 ? "#00C087"
    : pcr < 0.7 ? "#34d399"
    : "#94a3b8";

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "1fr 40px 60px 60px",
        padding: "5px 10px",
        borderBottom: "1px solid #1a1d24",
        fontSize: 11,
        alignItems: "center",
        gap: 4,
      }}
    >
      <span style={{ color: "#94a3b8" }}>{ep.expiry}</span>
      <span style={{ color: "#64748b", textAlign: "right" }}>{ep.days_to_expiry}d</span>
      <span style={{ color: "#e2e8f0", textAlign: "right" }}>
        {fmtVol(ep.call_volume)} / {fmtVol(ep.put_volume)}
      </span>
      <span style={{ color, textAlign: "right", fontWeight: 600 }}>
        {fmtPCR(ep.volume_pcr)}
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export default function PutCallRatioPanel() {
  const [inputTicker, setInputTicker] = useState("SPY");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<PCRData | null>(null);

  async function handleQuery() {
    if (!inputTicker.trim()) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const data = await fetchPutCallRatio(inputTicker.trim().toUpperCase());
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
        <span>Put/Call Ratio 期权情绪</span>
        <span style={{ fontSize: 10, color: "#475569", fontWeight: 400 }}>
          Volume PCR · OI PCR · 到期日明细
        </span>
      </div>

      {/* 查询栏 */}
      <div style={{ display: "flex", gap: 8, marginBottom: 14, alignItems: "center" }}>
        <input
          value={inputTicker}
          onChange={(e) => setInputTicker(e.target.value.toUpperCase())}
          onKeyDown={(e) => e.key === "Enter" && handleQuery()}
          placeholder="SPY"
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

          {/* 情绪徽章 */}
          {result.sentiment !== "unknown" && (
            <div style={{ marginBottom: 12 }}>
              <span
                style={{
                  display: "inline-block",
                  background: sentimentColor(result.sentiment) + "22",
                  border: `1px solid ${sentimentColor(result.sentiment)}55`,
                  color: sentimentColor(result.sentiment),
                  borderRadius: 5,
                  padding: "3px 10px",
                  fontSize: 12,
                  fontWeight: 700,
                }}
              >
                {sentimentLabel(result.sentiment)}
              </span>
            </div>
          )}

          {/* PCR 仪表盘 */}
          <div
            style={{
              background: "#151619",
              border: "1px solid #2a2d35",
              borderRadius: 8,
              padding: "10px 14px",
              marginBottom: 10,
            }}
          >
            <PCRGauge pcr={result.volume_pcr} />

            {/* 关键数字 */}
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
                  {fmtPCR(result.volume_pcr)}
                </div>
                <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>Volume PCR</div>
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
                  {fmtPCR(result.oi_pcr)}
                </div>
                <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>OI PCR</div>
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
                <div style={{ fontSize: 12, fontWeight: 700, color: "#34d399" }}>
                  {fmtVol(result.total_call_volume)}
                </div>
                <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>Call量</div>
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
                <div style={{ fontSize: 12, fontWeight: 700, color: "#ef4444" }}>
                  {fmtVol(result.total_put_volume)}
                </div>
                <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>Put量</div>
              </div>
            </div>
          </div>

          {/* 到期日明细 */}
          {result.expiry_breakdown.length > 0 && (
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
                  display: "grid",
                  gridTemplateColumns: "1fr 40px 60px 60px",
                  padding: "5px 10px",
                  background: "#1a1d24",
                  fontSize: 9,
                  color: "#475569",
                  fontWeight: 600,
                  gap: 4,
                }}
              >
                <span>到期日</span>
                <span style={{ textAlign: "right" }}>DTE</span>
                <span style={{ textAlign: "right" }}>Call/Put 量</span>
                <span style={{ textAlign: "right" }}>PCR</span>
              </div>
              {result.expiry_breakdown.map((ep, i) => (
                <ExpiryRow key={i} ep={ep} />
              ))}
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
            <div>PCR = Put 成交量 / Call 成交量；&gt; 1.5 极度恐慌，反向偏多</div>
            <div>PCR &lt; 0.5 极度贪婪，历史上往往是短期顶部信号（反向偏空）</div>
            <div>OI PCR 反映市场中线持仓分布，变化更缓慢</div>
          </div>

          <div style={{ marginTop: 8, fontSize: 10, color: "#475569" }}>
            数据日期：{result.as_of_date}　│　来源：yfinance　│　不构成投资建议
          </div>
        </div>
      )}

      {!result && !loading && !error && (
        <div
          style={{
            textAlign: "center",
            color: "#475569",
            fontSize: 12,
            padding: "20px 0",
          }}
        >
          输入股票或 ETF 代码查询 Put/Call Ratio
        </div>
      )}
    </div>
  );
}

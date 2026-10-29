/**
 * ShortInterestPanel — 空头兴趣 + 轧空风险面板
 *
 * 功能：
 * - 展示单只股票空头占比、DTC、融券变化
 * - 轧空风险评分（0-1）+ 信号标签
 * - 批量扫描（最多 20 只，按评分排序）
 *
 * Phase F.9 — #0E1014 / #151619 / #00C087 dark-theme tokens
 */
import { useState } from "react";
import {
  type ShortInterestData,
  fetchShortInterestSummary,
  fetchSqueezeScan,
} from "../api/client";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

type SqueezeSignal = ShortInterestData["signal"];

function signalColor(s: SqueezeSignal): string {
  if (s === "squeeze_setup") return "#ef4444";
  if (s === "high_short") return "#f59e0b";
  if (s === "moderate") return "#64748b";
  return "#00C087"; // low_short
}

function signalCN(s: SqueezeSignal): string {
  const map: Record<SqueezeSignal, string> = {
    squeeze_setup: "轧空风险",
    high_short: "高空头",
    moderate: "中等",
    low_short: "低空头",
  };
  return map[s] ?? s;
}

function fmtPct(n: number | null): string {
  if (n == null) return "—";
  return `${(n * 100).toFixed(1)}%`;
}

function fmtNum(n: number | null): string {
  if (n == null) return "—";
  if (Math.abs(n) >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  return n.toFixed(1);
}

// ---------------------------------------------------------------------------
// Score gauge
// ---------------------------------------------------------------------------

function ScoreGauge({ score, signal }: { score: number; signal: SqueezeSignal }) {
  const color = signalColor(signal);
  const pct = Math.round(score * 100);
  return (
    <div style={{ marginBottom: 12 }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          fontSize: 11,
          color: "#64748b",
          marginBottom: 4,
        }}
      >
        <span>轧空风险评分</span>
        <span style={{ color, fontWeight: 700 }}>{pct} / 100</span>
      </div>
      <div
        style={{
          height: 8,
          background: "#2a2d35",
          borderRadius: 4,
          overflow: "hidden",
        }}
      >
        <div
          style={{
            width: `${pct}%`,
            height: "100%",
            background: color,
            borderRadius: 4,
            transition: "width 0.3s ease",
          }}
        />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Detail card for a single ticker
// ---------------------------------------------------------------------------

function DetailCard({ d }: { d: ShortInterestData }) {
  const sc = signalColor(d.signal);
  const sl = signalCN(d.signal);

  const rows: [string, string][] = [
    ["空头占流通股比例", fmtPct(d.short_pct_float)],
    ["空头回补天数（DTC）", d.short_ratio != null ? d.short_ratio.toFixed(1) : "—"],
    ["融券股数", fmtNum(d.shares_short)],
    ["上月融券股数", fmtNum(d.shares_short_prior_month)],
    ["月环比变化", d.short_change_pct != null ? (d.short_change_pct >= 0 ? "+" : "") + fmtPct(d.short_change_pct) : "—"],
    ["流通股总数", fmtNum(d.float_shares)],
    ["10 日均量", fmtNum(d.avg_daily_volume)],
    ["现价 vs 52 周高", fmtPct(d.price_vs_52w_high)],
  ];

  return (
    <div
      style={{
        background: "#151619",
        border: `1px solid ${sc}33`,
        borderRadius: 8,
        padding: "12px 14px",
        marginBottom: 10,
      }}
    >
      {/* 头部 */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 10,
        }}
      >
        <div style={{ fontWeight: 700, fontSize: 16, color: "#e2e8f0" }}>
          {d.ticker}
        </div>
        <div
          style={{
            fontSize: 11,
            padding: "2px 8px",
            borderRadius: 4,
            background: sc + "22",
            color: sc,
            fontWeight: 700,
          }}
        >
          {sl}
        </div>
      </div>

      <ScoreGauge score={d.squeeze_risk_score} signal={d.signal} />

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
// Scan table row
// ---------------------------------------------------------------------------

function ScanRow({ d }: { d: ShortInterestData }) {
  const sc = signalColor(d.signal);
  const sl = signalCN(d.signal);
  const pct = Math.round(d.squeeze_risk_score * 100);
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "80px 1fr 90px 80px",
        gap: 8,
        alignItems: "center",
        padding: "6px 10px",
        borderBottom: "1px solid #2a2d35",
      }}
    >
      <div style={{ fontWeight: 700, fontSize: 12, color: "#e2e8f0" }}>{d.ticker}</div>
      <div style={{ height: 5, background: "#2a2d35", borderRadius: 3, overflow: "hidden" }}>
        <div style={{ width: `${pct}%`, height: "100%", background: sc, borderRadius: 3 }} />
      </div>
      <div style={{ fontSize: 11, color: sc, textAlign: "right", fontWeight: 700 }}>
        {pct}/100
      </div>
      <div
        style={{
          fontSize: 10,
          padding: "2px 6px",
          borderRadius: 4,
          background: sc + "22",
          color: sc,
          textAlign: "center",
          fontWeight: 700,
        }}
      >
        {sl}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function ShortInterestPanel() {
  const [mode, setMode] = useState<"single" | "scan">("single");
  const [inputTicker, setInputTicker] = useState("GME");
  const [scanTickers, setScanTickers] = useState("GME,AMC,BB,BBBY,KOSS");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [singleResult, setSingleResult] = useState<ShortInterestData | null>(null);
  const [scanResult, setScanResult] = useState<ShortInterestData[]>([]);

  async function handleSingle() {
    const t = inputTicker.trim().toUpperCase();
    if (!t) return;
    setLoading(true);
    setError(null);
    setSingleResult(null);
    try {
      const data = await fetchShortInterestSummary(t);
      setSingleResult(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "查询失败");
    } finally {
      setLoading(false);
    }
  }

  async function handleScan() {
    const tickers = scanTickers
      .split(/[,\s]+/)
      .map((t) => t.trim().toUpperCase())
      .filter(Boolean);
    if (!tickers.length) return;
    setLoading(true);
    setError(null);
    setScanResult([]);
    try {
      const data = await fetchSqueezeScan(tickers);
      setScanResult(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "扫描失败");
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
        <span>空头兴趣 + 轧空风险（Short Interest）</span>
      </div>

      {/* 模式切换 */}
      <div style={{ display: "flex", gap: 8, marginBottom: 14 }}>
        {(["single", "scan"] as const).map((m) => (
          <button
            key={m}
            onClick={() => setMode(m)}
            style={{
              background: mode === m ? "#00C087" : "#1f2937",
              color: mode === m ? "#0E1014" : "#94a3b8",
              border: "none",
              borderRadius: 6,
              padding: "4px 14px",
              fontWeight: 700,
              fontSize: 12,
              cursor: "pointer",
            }}
          >
            {m === "single" ? "单只查询" : "批量扫描"}
          </button>
        ))}
      </div>

      {/* 单只查询 */}
      {mode === "single" && (
        <div style={{ display: "flex", gap: 8, marginBottom: 14, alignItems: "center" }}>
          <input
            value={inputTicker}
            onChange={(e) => setInputTicker(e.target.value.toUpperCase())}
            onKeyDown={(e) => e.key === "Enter" && handleSingle()}
            placeholder="GME"
            style={{ ...inputStyle, width: 120 }}
          />
          <button
            onClick={handleSingle}
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
      )}

      {/* 批量扫描 */}
      {mode === "scan" && (
        <div style={{ display: "flex", gap: 8, marginBottom: 14, alignItems: "center" }}>
          <input
            value={scanTickers}
            onChange={(e) => setScanTickers(e.target.value.toUpperCase())}
            placeholder="GME,AMC,BB（最多 20 只）"
            style={{ ...inputStyle, flex: 1 }}
          />
          <button
            onClick={handleScan}
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
            {loading ? "扫描中..." : "扫描"}
          </button>
        </div>
      )}

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

      {/* 单只结果 */}
      {mode === "single" && singleResult && (
        <>
          <DetailCard d={singleResult} />
          <div style={{ marginTop: 8, fontSize: 10, color: "#475569" }}>
            数据日期：{singleResult.as_of_date}　│　来源：yfinance　│　不构成投资建议
          </div>
        </>
      )}

      {/* 批量扫描结果 */}
      {mode === "scan" && scanResult.length > 0 && (
        <div
          style={{
            background: "#151619",
            border: "1px solid #2a2d35",
            borderRadius: 8,
            overflow: "hidden",
          }}
        >
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "80px 1fr 90px 80px",
              gap: 8,
              padding: "6px 10px",
              background: "#0E1014",
              fontSize: 10,
              color: "#64748b",
              fontWeight: 700,
              textTransform: "uppercase",
            }}
          >
            <div>代码</div>
            <div>评分</div>
            <div style={{ textAlign: "right" }}>分数</div>
            <div style={{ textAlign: "center" }}>信号</div>
          </div>
          {scanResult.map((d) => (
            <ScanRow key={d.ticker} d={d} />
          ))}
        </div>
      )}

      {/* 空态 */}
      {!loading && !error && (
        (mode === "single" && !singleResult) ||
        (mode === "scan" && !scanResult.length)
      ) && (
        <div
          style={{
            textAlign: "center",
            color: "#475569",
            fontSize: 12,
            padding: "20px 0",
          }}
        >
          {mode === "single"
            ? "输入股票代码后点击「查询」，获取空头兴趣与轧空风险评分"
            : "输入逗号分隔的股票代码后点击「扫描」，批量获取轧空风险排名"}
        </div>
      )}
    </div>
  );
}

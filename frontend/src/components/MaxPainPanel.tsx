/**
 * MaxPainPanel — 期权最大痛苦值面板
 *
 * 功能（需用户输入 ticker）：
 * - 每个近期到期日的 Max Pain 价格
 * - 当前价格 vs Max Pain 距离 + 信号
 * - 信号徽章：pin_zone（金色）/ bullish_pull（绿）/ bearish_pull（红）/ weak_pull（灰）
 * - 可视化：当前价格与 Max Pain 的相对位置条
 *
 * Phase F.29 — Max Pain Calculator
 * 数据来源：yfinance 期权链（免费）
 */

import { useState } from "react";
import {
  type ExpiryMaxPain,
  type MaxPainData,
  type MaxPainSignal,
  fetchMaxPain,
} from "../api/client";

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function signalColor(s: MaxPainSignal): string {
  if (s === "pin_zone") return "#f59e0b";
  if (s === "bullish_pull") return "#00C087";
  if (s === "bearish_pull") return "#ef4444";
  if (s === "weak_pull") return "#475569";
  return "#475569";
}

function signalLabel(s: MaxPainSignal): string {
  const labels: Record<MaxPainSignal, string> = {
    pin_zone: "⚡ 钉子区（Pin Zone）",
    bullish_pull: "↑ 上拉力（价格偏低）",
    bearish_pull: "↓ 下压力（价格偏高）",
    weak_pull: "弱磁力（距离远）",
    unknown: "—",
  };
  return labels[s];
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

/** Horizontal bar showing current price vs max pain */
function PriceVsMaxPainBar({
  currentPrice,
  maxPainStrike,
  distancePct,
}: {
  currentPrice: number;
  maxPainStrike: number;
  distancePct: number;
}) {
  // Show a bar where center = max pain, current price is offset left/right
  const absMax = Math.max(Math.abs(distancePct), 1);
  const barWidth = Math.min(Math.abs(distancePct) / absMax, 1) * 45; // max 45% each side

  const isAbove = distancePct < 0; // price above max pain

  return (
    <div style={{ margin: "4px 0" }}>
      <div
        style={{
          position: "relative",
          height: 16,
          background: "#1a1d24",
          borderRadius: 4,
          overflow: "hidden",
        }}
      >
        {/* Center marker */}
        <div
          style={{
            position: "absolute",
            left: "50%",
            top: 0,
            bottom: 0,
            width: 2,
            background: "#f59e0b",
            transform: "translateX(-50%)",
          }}
        />
        {/* Price position bar */}
        <div
          style={{
            position: "absolute",
            top: 3,
            bottom: 3,
            left: isAbove ? `${50 - barWidth}%` : "50%",
            width: `${barWidth}%`,
            background: isAbove ? "#ef4444" : "#00C087",
            borderRadius: 3,
            opacity: 0.7,
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
        <span>当前 ${currentPrice.toFixed(2)}</span>
        <span style={{ color: "#f59e0b" }}>Max Pain ${maxPainStrike.toFixed(2)}</span>
        <span
          style={{
            color: distancePct >= 0 ? "#00C087" : "#ef4444",
          }}
        >
          {distancePct >= 0 ? "+" : ""}
          {distancePct.toFixed(1)}%
        </span>
      </div>
    </div>
  );
}

function ExpiryRow({ ep }: { ep: ExpiryMaxPain }) {
  const color = signalColor(ep.signal);
  return (
    <div
      style={{
        background: "#0E1014",
        border: `1px solid ${ep.signal === "pin_zone" ? "#f59e0b44" : "#2a2d35"}`,
        borderRadius: 6,
        padding: "8px 12px",
        marginBottom: 6,
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 6,
        }}
      >
        <div>
          <span style={{ fontSize: 12, fontWeight: 700, color: "#e2e8f0" }}>
            {ep.expiry}
          </span>
          <span
            style={{
              fontSize: 10,
              color: "#64748b",
              marginLeft: 8,
            }}
          >
            DTE {ep.dte}
          </span>
        </div>
        <span
          style={{
            fontSize: 10,
            fontWeight: 700,
            background: color + "22",
            border: `1px solid ${color}55`,
            color,
            borderRadius: 4,
            padding: "1px 7px",
          }}
        >
          {signalLabel(ep.signal)}
        </span>
      </div>

      <PriceVsMaxPainBar
        currentPrice={ep.current_price}
        maxPainStrike={ep.max_pain_strike}
        distancePct={ep.distance_pct}
      />

      <div
        style={{
          display: "flex",
          gap: 12,
          marginTop: 4,
          fontSize: 10,
          color: "#64748b",
        }}
      >
        <span>Call OI: {ep.total_call_oi.toLocaleString()}</span>
        <span>Put OI: {ep.total_put_oi.toLocaleString()}</span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export default function MaxPainPanel() {
  const [ticker, setTicker] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<MaxPainData | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!ticker.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const data = await fetchMaxPain(ticker.trim().toUpperCase());
      setResult(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }

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
        <span>期权最大痛苦值（Max Pain）</span>
        <span style={{ fontSize: 10, color: "#475569", fontWeight: 400 }}>
          钉子区 · 磁力方向 · 到期日分析
        </span>
      </div>

      {/* 输入 */}
      <form
        onSubmit={handleSubmit}
        style={{ display: "flex", gap: 8, marginBottom: 12 }}
      >
        <input
          value={ticker}
          onChange={(e) => setTicker(e.target.value.toUpperCase())}
          placeholder="Ticker（如 AAPL、SPY、QQQ）"
          style={{
            flex: 1,
            background: "#151619",
            border: "1px solid #2a2d35",
            borderRadius: 5,
            color: "#e2e8f0",
            padding: "5px 10px",
            fontSize: 12,
            fontFamily: "ui-monospace, monospace",
          }}
        />
        <button
          type="submit"
          disabled={loading || !ticker.trim()}
          style={{
            background: loading ? "#1a1d24" : "#1a2a3a",
            border: "1px solid #2a2d35",
            borderRadius: 5,
            color: loading ? "#475569" : "#60a5fa",
            padding: "5px 14px",
            fontSize: 12,
            cursor: loading || !ticker.trim() ? "not-allowed" : "pointer",
          }}
        >
          {loading ? "计算中..." : "计算"}
        </button>
      </form>

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

          {/* 当前价格摘要 */}
          {result.current_price != null && (
            <div
              style={{
                background: "#151619",
                border: "1px solid #2a2d35",
                borderRadius: 8,
                padding: "8px 14px",
                marginBottom: 10,
                display: "flex",
                alignItems: "center",
                gap: 12,
              }}
            >
              <span style={{ fontSize: 10, color: "#64748b" }}>
                {result.ticker} 当前价格
              </span>
              <span style={{ fontSize: 16, fontWeight: 700, color: "#e2e8f0" }}>
                ${result.current_price.toFixed(2)}
              </span>
            </div>
          )}

          {/* 到期日列表 */}
          {result.expiries.length === 0 ? (
            <div style={{ color: "#64748b", fontSize: 12, padding: "8px 0" }}>
              无近期期权数据（DTE 1-45）
            </div>
          ) : (
            <div>
              {result.expiries.map((ep) => (
                <ExpiryRow key={ep.expiry} ep={ep} />
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
            <div>Max Pain = 所有期权买方总损失最大的价格（做市商 delta hedge 目标）</div>
            <div>
              ⚡ Pin Zone（距离 &lt; 2%）：价格被钉住概率高，适合卖方策略
            </div>
            <div>
              ↑↓ 磁力方向：价格偏低→上拉；价格偏高→下压（越近越效果越强）
            </div>
          </div>

          <div style={{ marginTop: 8, fontSize: 10, color: "#475569" }}>
            数据日期：{result.as_of_date}　│　来源：yfinance　│　不构成投资建议
          </div>
        </div>
      )}
    </div>
  );
}

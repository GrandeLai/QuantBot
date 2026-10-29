/**
 * EarningsCalendarPanel — 财报日历与期权预期波动面板
 *
 * 功能：
 * - 下次财报日期倒计时
 * - ATM Straddle 成本（期权隐含预期波动）
 * - 历史实际波动平均值对比
 * - 买入/卖出 Straddle 信号
 * - 最近 8 次历史财报波动记录
 *
 * Phase F.25 — Earnings Calendar & Options Expected Move
 * 数据来源：yfinance 期权链 + 价格历史 + 财报日历
 */
import { useState } from "react";
import {
  type EarningsCalendarData,
  type EarningsMove,
  type StraddleSignal,
  fetchEarningsCalendar,
} from "../api/client";

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function signalColor(signal: StraddleSignal): string {
  if (signal === "buy_straddle") return "#00C087";
  if (signal === "sell_straddle") return "#ef4444";
  if (signal === "fair") return "#f59e0b";
  return "#475569";
}

function signalLabel(signal: StraddleSignal): string {
  if (signal === "buy_straddle") return "★ 买入 Straddle（期权偏便宜）";
  if (signal === "sell_straddle") return "✗ 卖出 Straddle / Iron Condor（期权偏贵）";
  if (signal === "fair") return "△ 合理定价，观望";
  return "—";
}

function moveColor(pct: number): string {
  if (pct > 0) return "#00C087";
  if (pct < 0) return "#ef4444";
  return "#94a3b8";
}

function fmtPct(v: number | null, decimals = 1): string {
  if (v == null) return "—";
  return `${v >= 0 ? "+" : ""}${v.toFixed(decimals)}%`;
}

function fmtAbsPct(v: number | null): string {
  if (v == null) return "—";
  return `±${v.toFixed(1)}%`;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function MoveComparisonBar({
  implied,
  historical,
}: {
  implied: number | null;
  historical: number | null;
}) {
  if (implied == null && historical == null) return null;
  const max = Math.max(implied ?? 0, historical ?? 0, 0.1) * 1.2;

  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ fontSize: 10, color: "#475569", marginBottom: 6, fontWeight: 600 }}>
        波动幅度对比
      </div>
      {implied != null && (
        <div style={{ marginBottom: 4 }}>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 2 }}>
            <span style={{ fontSize: 10, color: "#94a3b8" }}>隐含预期</span>
            <span style={{ fontSize: 11, fontWeight: 700, color: "#e2e8f0" }}>
              {fmtAbsPct(implied)}
            </span>
          </div>
          <div style={{ background: "#1a1d24", borderRadius: 3, height: 6 }}>
            <div
              style={{
                width: `${(implied / max) * 100}%`,
                height: "100%",
                background: "#f59e0b",
                borderRadius: 3,
              }}
            />
          </div>
        </div>
      )}
      {historical != null && (
        <div>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 2 }}>
            <span style={{ fontSize: 10, color: "#94a3b8" }}>历史均值</span>
            <span style={{ fontSize: 11, fontWeight: 700, color: "#e2e8f0" }}>
              {fmtAbsPct(historical)}
            </span>
          </div>
          <div style={{ background: "#1a1d24", borderRadius: 3, height: 6 }}>
            <div
              style={{
                width: `${(historical / max) * 100}%`,
                height: "100%",
                background: "#64748b",
                borderRadius: 3,
              }}
            />
          </div>
        </div>
      )}
    </div>
  );
}

function HistoricalMoveRow({ move }: { move: EarningsMove }) {
  return (
    <div
      style={{
        display: "flex",
        justifyContent: "space-between",
        padding: "4px 10px",
        borderBottom: "1px solid #1a1d24",
        fontSize: 11,
        alignItems: "center",
      }}
    >
      <span style={{ color: "#64748b", fontSize: 10 }}>{move.date}</span>
      <span
        style={{
          fontWeight: 600,
          color: moveColor(move.actual_move_pct),
          minWidth: 60,
          textAlign: "right",
        }}
      >
        {fmtPct(move.actual_move_pct)}
      </span>
      <span style={{ color: "#94a3b8", fontSize: 10, minWidth: 50, textAlign: "right" }}>
        {fmtAbsPct(move.abs_move_pct)}
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export default function EarningsCalendarPanel() {
  const [inputTicker, setInputTicker] = useState("AAPL");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<EarningsCalendarData | null>(null);

  async function handleQuery() {
    if (!inputTicker.trim()) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const data = await fetchEarningsCalendar(inputTicker.trim().toUpperCase());
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
        <span>财报日历与预期波动面板</span>
        <span style={{ fontSize: 10, color: "#475569", fontWeight: 400 }}>
          Straddle · 隐含 vs 历史波动
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
          {result.straddle_signal !== "unknown" && (
            <div style={{ marginBottom: 12 }}>
              <span
                style={{
                  display: "inline-block",
                  background: signalColor(result.straddle_signal) + "22",
                  border: `1px solid ${signalColor(result.straddle_signal)}55`,
                  color: signalColor(result.straddle_signal),
                  borderRadius: 5,
                  padding: "3px 10px",
                  fontSize: 12,
                  fontWeight: 700,
                }}
              >
                {signalLabel(result.straddle_signal)}
              </span>
            </div>
          )}

          {/* 财报日期卡片 */}
          <div
            style={{
              background: "#151619",
              border: "1px solid #2a2d35",
              borderRadius: 8,
              padding: "10px 14px",
              marginBottom: 10,
            }}
          >
            <div style={{ display: "flex", gap: 8, marginBottom: 10 }}>
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
                <div style={{ fontSize: 13, fontWeight: 700, color: "#e2e8f0" }}>
                  {result.next_earnings_date ?? "—"}
                </div>
                <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>下次财报</div>
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
                <div
                  style={{
                    fontSize: 15,
                    fontWeight: 700,
                    color:
                      result.days_to_earnings != null && result.days_to_earnings <= 7
                        ? "#f59e0b"
                        : "#e2e8f0",
                  }}
                >
                  {result.days_to_earnings != null ? `${result.days_to_earnings}天` : "—"}
                </div>
                <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>倒计时</div>
              </div>
            </div>

            {/* 波动幅度对比 */}
            <MoveComparisonBar
              implied={result.implied_move_pct}
              historical={result.historical_avg_move_pct}
            />
          </div>

          {/* 历史财报波动记录 */}
          {result.historical_moves.length > 0 && (
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
                <span>历史财报日期</span>
                <span>实际涨跌</span>
                <span>绝对幅度</span>
              </div>
              {result.historical_moves.map((m, i) => (
                <HistoricalMoveRow key={i} move={m} />
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
            <div>隐含预期 = ATM Straddle（call + put）成本 / 当前股价</div>
            <div>历史均值 = 最近 8 次财报后首日涨跌幅绝对值均值</div>
            <div>隐含 &gt; 1.5× 历史 → 期权偏贵；历史 &gt; 1.5× 隐含 → 期权偏便宜</div>
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
          输入股票代码查询财报日期与期权预期波动
        </div>
      )}
    </div>
  );
}

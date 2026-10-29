/**
 * EarningsMovePanel — 财报前期权隐含预期摆幅面板
 *
 * 功能：
 * - 显示下次财报日期 + 倒计时天数
 * - 期权市场隐含的 ±% 预期波动（ATM straddle / current price）
 * - 等级 badge（大幅 / 中幅 / 小幅 / 无数据）
 * - 数据不可用降级提示
 *
 * Phase F.17 — 期权 IV 定价 alpha
 * 公式：expected_move = (ATM_call + ATM_put) / current_price × 100%
 */
import React, { useState } from "react";
import {
  type EarningsMoveData,
  type EarningsMoveGrade,
  fetchEarningsMove,
} from "../api/client";

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function gradeColor(grade: EarningsMoveGrade): string {
  if (grade === "large_expected") return "#ef4444";
  if (grade === "medium_expected") return "#f59e0b";
  if (grade === "small_expected") return "#00C087";
  return "#64748b"; // no_data
}

function gradeLabel(grade: EarningsMoveGrade): string {
  const labels: Record<EarningsMoveGrade, string> = {
    large_expected: "大幅波动预期 ⚡",
    medium_expected: "中幅波动预期",
    small_expected: "小幅波动预期",
    no_data: "数据不可用",
  };
  return labels[grade];
}

function daysLabel(days: number | null): string {
  if (days === null) return "—";
  if (days < 0) return `已过 ${Math.abs(days)} 天`;
  if (days === 0) return "今天";
  if (days === 1) return "明天";
  return `${days} 天后`;
}

// ---------------------------------------------------------------------------
// 子组件：Expected Move 仪表
// ---------------------------------------------------------------------------

function MoveMeter({ pct }: { pct: number }) {
  // Cap visual at 20% for bar display
  const cappedPct = Math.min(pct, 20.0);
  const fillWidth = (cappedPct / 20.0) * 100;
  const color =
    pct > 10 ? "#ef4444" : pct > 5 ? "#f59e0b" : "#00C087";

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
          期权隐含预期摆幅
        </div>
        <span style={{ fontSize: 20, fontWeight: 700, color }}>
          ±{pct.toFixed(1)}%
        </span>
      </div>

      {/* Bar */}
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
            width: `${fillWidth}%`,
            height: "100%",
            background: `linear-gradient(90deg, #00C087, ${color})`,
            borderRadius: 4,
            transition: "width 0.4s ease",
          }}
        />
        {/* 5% threshold line */}
        <div
          style={{
            position: "absolute",
            left: "25%",
            top: -2,
            bottom: -2,
            width: 1,
            background: "#475569",
          }}
        />
        {/* 10% threshold line */}
        <div
          style={{
            position: "absolute",
            left: "50%",
            top: -2,
            bottom: -2,
            width: 1,
            background: "#475569",
          }}
        />
      </div>

      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 9, color: "#475569" }}>
        <span>0%</span>
        <span>5%</span>
        <span>10%</span>
        <span>20%+</span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export default function EarningsMovePanel() {
  const [inputTicker, setInputTicker] = useState("AAPL");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<EarningsMoveData | null>(null);

  async function handleQuery() {
    if (!inputTicker.trim()) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const data = await fetchEarningsMove(inputTicker.trim().toUpperCase());
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
        <span>财报预期摆幅（ATM Straddle）</span>
        <span style={{ fontSize: 10, color: "#475569", fontWeight: 400 }}>
          来源：yfinance · ATM straddle implied move
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
              ⚠ 期权链或财报日期数据不可用（可能无期权或财报日期未确定）。
            </div>
          )}

          {/* 财报日期卡片 */}
          {result.next_earnings_date && (
            <div
              style={{
                background: "#151619",
                border: "1px solid #2a2d35",
                borderRadius: 8,
                padding: "10px 14px",
                marginBottom: 10,
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
              }}
            >
              <div>
                <div style={{ fontSize: 10, color: "#64748b", marginBottom: 2 }}>
                  下次财报日期
                </div>
                <div style={{ fontSize: 16, fontWeight: 700, color: "#e2e8f0" }}>
                  {result.next_earnings_date}
                </div>
              </div>
              <div
                style={{
                  fontSize: 22,
                  fontWeight: 700,
                  color: result.days_to_earnings !== null && result.days_to_earnings <= 7
                    ? "#f59e0b"
                    : "#94a3b8",
                }}
              >
                {daysLabel(result.days_to_earnings)}
              </div>
            </div>
          )}

          {/* Grade badge */}
          <div
            style={{
              background: "#151619",
              border: "1px solid #2a2d35",
              borderRadius: 8,
              padding: "10px 14px",
              marginBottom: 10,
              display: "flex",
              alignItems: "center",
              gap: 12,
            }}
          >
            <span
              style={{
                fontSize: 14,
                fontWeight: 700,
                color: gradeColor(result.grade),
                padding: "2px 10px",
                borderRadius: 4,
                background: gradeColor(result.grade) + "22",
              }}
            >
              {gradeLabel(result.grade)}
            </span>
            {result.current_price && (
              <span style={{ fontSize: 12, color: "#64748b" }}>
                当前价：${result.current_price.toFixed(2)}
              </span>
            )}
          </div>

          {/* Expected Move 仪表 */}
          {result.expected_move_pct !== null && (
            <MoveMeter pct={result.expected_move_pct} />
          )}

          {/* Straddle 细节 */}
          {result.straddle_price !== null && result.atm_strike !== null && (
            <div
              style={{
                display: "flex",
                gap: 6,
                marginBottom: 10,
              }}
            >
              {[
                { label: "ATM Strike", value: `$${result.atm_strike.toFixed(1)}` },
                { label: "Straddle 价格", value: `$${result.straddle_price.toFixed(2)}` },
                { label: "预期摆幅", value: `±${result.expected_move_pct?.toFixed(1)}%` },
              ].map(({ label, value }) => (
                <div
                  key={label}
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
                    {value}
                  </div>
                  <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>{label}</div>
                </div>
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
            <div>ATM Straddle = 最近行权价 Call + Put 价格之和；除以股价得到期权隐含 ±1σ 财报摆幅</div>
            <div>期权卖方（sell straddle）需要实际波动小于此值才能盈利</div>
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
          输入股票代码查询财报前期权隐含预期摆幅（ATM straddle implied move）
        </div>
      )}
    </div>
  );
}

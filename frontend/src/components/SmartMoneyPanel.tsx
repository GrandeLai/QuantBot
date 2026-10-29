/**
 * SmartMoneyPanel — 大单不对称积分 / 机构资金流向面板
 *
 * 功能：
 * - Smart Money 信号 badge（6 种）
 * - 今日大单买压比 vs 5 日均值（百分比条）
 * - 5 天日度大单流向柱状图（买 vs 卖）
 * - 大单阈值说明
 *
 * Phase F.22 — Smart Money Flow
 * 来源：yfinance 1 分钟 K 线（最近 5 个交易日）
 */
import { useState } from "react";
import {
  type DailyFlowItem,
  type SmartMoneyData,
  type SmartMoneySignal,
  fetchSmartMoney,
} from "../api/client";

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function signalColor(signal: SmartMoneySignal): string {
  if (signal === "smart_money_buy") return "#00C087";
  if (signal === "smart_money_sell") return "#ef4444";
  if (signal === "accumulation") return "#34d399";
  if (signal === "distribution") return "#f87171";
  if (signal === "neutral") return "#64748b";
  return "#475569";
}

function signalLabel(signal: SmartMoneySignal): string {
  const labels: Record<SmartMoneySignal, string> = {
    smart_money_buy: "✓✓ 机构强买",
    smart_money_sell: "✗✗ 机构强卖",
    accumulation: "↑ 缓慢积累",
    distribution: "↓ 缓慢派发",
    neutral: "中性",
    no_data: "无数据",
  };
  return labels[signal];
}

function fmtUSD(n: number): string {
  if (n >= 1_000_000_000) return `$${(n / 1_000_000_000).toFixed(1)}B`;
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `$${(n / 1_000).toFixed(0)}K`;
  return `$${n.toFixed(0)}`;
}

// ---------------------------------------------------------------------------
// 子组件：买压比条形
// ---------------------------------------------------------------------------

function BuyPressureBar({
  todayPct,
  avgPct,
  label,
}: {
  todayPct: number;
  avgPct: number | null;
  label: string;
}) {
  const isStrong = todayPct > 60 || todayPct < 40;
  const color = todayPct > 55 ? "#00C087" : todayPct < 45 ? "#ef4444" : "#64748b";

  return (
    <div style={{ marginBottom: 10 }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          fontSize: 10,
          color: "#94a3b8",
          marginBottom: 4,
        }}
      >
        <span>{label}</span>
        <span style={{ color, fontWeight: isStrong ? 700 : 400 }}>
          {todayPct.toFixed(1)}%
          {avgPct != null && (
            <span style={{ color: "#475569", fontWeight: 400 }}>
              {" "}（5日均 {avgPct.toFixed(1)}%）
            </span>
          )}
        </span>
      </div>
      <div
        style={{
          position: "relative",
          height: 8,
          background: "#2a2d35",
          borderRadius: 4,
        }}
      >
        {/* Buy side */}
        <div
          style={{
            position: "absolute",
            left: 0,
            width: `${todayPct}%`,
            height: "100%",
            background: color,
            borderRadius: 4,
            transition: "width 0.4s ease",
          }}
        />
        {/* Average marker */}
        {avgPct != null && (
          <div
            style={{
              position: "absolute",
              left: `${avgPct}%`,
              top: -2,
              width: 2,
              height: 12,
              background: "#f59e0b",
              borderRadius: 1,
              transform: "translateX(-50%)",
            }}
          />
        )}
        {/* 50% center line */}
        <div
          style={{
            position: "absolute",
            left: "50%",
            top: 0,
            width: 1,
            height: "100%",
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
          marginTop: 2,
        }}
      >
        <span>100% 卖出</span>
        <span>50%</span>
        <span>100% 买入</span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 子组件：日度流向迷你柱
// ---------------------------------------------------------------------------

function DailyFlowBar({ flows }: { flows: DailyFlowItem[] }) {
  const maxUsd = Math.max(...flows.map((f) => f.total_large_usd), 1);

  return (
    <div>
      <div
        style={{
          fontSize: 10,
          color: "#475569",
          marginBottom: 6,
          display: "flex",
          justifyContent: "space-between",
        }}
      >
        <span>大单日度流向（5天）</span>
        <span>█ 买入 █ 卖出</span>
      </div>
      <div style={{ display: "flex", gap: 4, alignItems: "flex-end", height: 50 }}>
        {flows.map((f) => {
          const totalH = (f.total_large_usd / maxUsd) * 44;
          const buyH = f.total_large_usd > 0 ? (f.large_buy_usd / f.total_large_usd) * totalH : 0;
          const sellH = totalH - buyH;
          return (
            <div
              key={f.date}
              style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", gap: 1 }}
            >
              <div style={{ height: 50 - totalH }} />
              <div
                title={`买 ${fmtUSD(f.large_buy_usd)} / 卖 ${fmtUSD(f.large_sell_usd)}`}
                style={{ width: "100%", display: "flex", flexDirection: "column", gap: 1 }}
              >
                <div style={{ height: buyH, background: "#00C087", borderRadius: "2px 2px 0 0" }} />
                <div style={{ height: sellH, background: "#ef4444", borderRadius: "0 0 2px 2px" }} />
              </div>
              <div style={{ fontSize: 8, color: "#475569", marginTop: 2, textAlign: "center" }}>
                {f.date.slice(5)}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export default function SmartMoneyPanel() {
  const [inputTicker, setInputTicker] = useState("AAPL");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<SmartMoneyData | null>(null);

  async function handleQuery() {
    if (!inputTicker.trim()) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const data = await fetchSmartMoney(inputTicker.trim().toUpperCase());
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
        <span>大单不对称积分 (Smart Money Flow)</span>
        <span style={{ fontSize: 10, color: "#475569", fontWeight: 400 }}>
          来源：yfinance 1 分钟 K 线 · 近 5 交易日
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
              ⚠ 分钟级数据不可用（yfinance 仅支持最近 7 天内 1 分钟 K 线）。
            </div>
          )}

          {/* 信号 badge */}
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
            <span
              style={{
                fontSize: 16,
                fontWeight: 700,
                color: signalColor(result.signal),
                padding: "2px 10px",
                borderRadius: 4,
                background: signalColor(result.signal) + "22",
              }}
            >
              {signalLabel(result.signal)}
            </span>
            <div style={{ textAlign: "right" }}>
              {result.large_threshold_usd != null && (
                <div style={{ fontSize: 11, color: "#94a3b8" }}>
                  大单阈值 {fmtUSD(result.large_threshold_usd)}/bar
                </div>
              )}
              <div style={{ fontSize: 10, color: "#475569" }}>2× 中位成交量</div>
            </div>
          </div>

          {/* 买压比条形 */}
          {result.today_buy_pressure_pct != null && (
            <div
              style={{
                background: "#151619",
                border: "1px solid #2a2d35",
                borderRadius: 8,
                padding: "10px 14px",
                marginBottom: 10,
              }}
            >
              <BuyPressureBar
                todayPct={result.today_buy_pressure_pct}
                avgPct={result.avg_5d_buy_pressure_pct}
                label="大单买压比（今日）"
              />
            </div>
          )}

          {/* 日度流向迷你图 */}
          {result.daily_flows.length > 0 && (
            <div
              style={{
                background: "#151619",
                border: "1px solid #2a2d35",
                borderRadius: 8,
                padding: "10px 14px",
                marginBottom: 10,
              }}
            >
              <DailyFlowBar flows={result.daily_flows} />
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
            <div>大单 = 美元成交额 &gt; 2× 5日中位数；买入 = close&gt;open，卖出 = close&lt;open</div>
            <div>信号阈值：买压比 &gt;60%（+10 pp vs 5日均）→ 机构强买；&lt;40%（-10 pp）→ 机构强卖</div>
            <div>数据为 yfinance 1 分钟 K 线，最近 7 天内可用（盘中延迟约 15 分钟）</div>
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
          输入股票代码查询大单不对称积分信号（识别机构资金进出方向）
        </div>
      )}
    </div>
  );
}

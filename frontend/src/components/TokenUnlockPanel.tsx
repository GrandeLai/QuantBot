/**
 * TokenUnlockPanel — 加密 Token 解锁日历 + 抛压模型
 *
 * 功能：
 * - 展示未来 N 天内的 token 解锁事件（DefiLlama 数据）
 * - 抛压评分（0-1）+ 信号分类（high_risk / moderate_risk / low_risk / post_unlock_rebound）
 * - 高风险预警卡片（score ≥ 0.6）
 * - 完整日历表格
 *
 * Phase F.7 — #0E1014 / #151619 / #00C087 dark-theme tokens
 */
import { useEffect, useState } from "react";
import {
  type TokenUnlockCalendarData,
  type TokenUnlockEventData,
  fetchTokenUnlocks,
} from "../api/client";

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function signalColor(sig: TokenUnlockEventData["signal"]): string {
  if (sig === "high_risk") return "#ef4444";
  if (sig === "moderate_risk") return "#f59e0b";
  if (sig === "post_unlock_rebound") return "#00C087";
  return "#64748b"; // low_risk
}

function signalLabel(sig: TokenUnlockEventData["signal"]): string {
  const map: Record<string, string> = {
    high_risk: "⚠ 高风险",
    moderate_risk: "⚡ 中等风险",
    low_risk: "✓ 低风险",
    post_unlock_rebound: "↑ 反弹窗口",
  };
  return map[sig] ?? sig;
}

function formatTokens(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return n.toFixed(0);
}

function formatUsd(n: number | null): string {
  if (n == null) return "—";
  if (n >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(1)}M`;
  return `$${n.toFixed(0)}`;
}

function categoryLabel(c: string): string {
  const map: Record<string, string> = {
    team: "团队",
    investors: "投资人",
    insiders: "内部人",
    advisors: "顾问",
    foundation: "基金会",
    ecosystem: "生态",
    community: "社区",
    public_sale: "公开销售",
    liquidity: "流动性",
    other: "其他",
  };
  return map[c] ?? c;
}

// ---------------------------------------------------------------------------
// 子组件：抛压进度条
// ---------------------------------------------------------------------------

function PressureBar({ score }: { score: number }) {
  const color =
    score >= 0.6 ? "#ef4444" : score >= 0.3 ? "#f59e0b" : "#64748b";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11 }}>
      <div
        style={{
          flex: 1,
          height: 5,
          background: "#2a2d35",
          borderRadius: 3,
          overflow: "hidden",
        }}
      >
        <div
          style={{
            width: `${Math.round(score * 100)}%`,
            height: "100%",
            background: color,
            borderRadius: 3,
          }}
        />
      </div>
      <span style={{ color, minWidth: 32, textAlign: "right" }}>
        {(score * 100).toFixed(0)}%
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 子组件：高风险预警卡片
// ---------------------------------------------------------------------------

function UnlockCard({ e }: { e: TokenUnlockEventData }) {
  const sc = signalColor(e.signal);
  return (
    <div
      style={{
        background: sc + "11",
        border: `1px solid ${sc}44`,
        borderRadius: 7,
        padding: "10px 12px",
        marginBottom: 6,
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          marginBottom: 6,
        }}
      >
        <div>
          <span
            style={{ fontSize: 14, fontWeight: 700, color: "#e2e8f0", marginRight: 6 }}
          >
            {e.symbol}
          </span>
          <span style={{ fontSize: 11, color: "#64748b" }}>{e.protocol}</span>
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
          {signalLabel(e.signal)}
        </div>
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr 1fr",
          gap: 6,
          marginBottom: 8,
          fontSize: 11,
        }}
      >
        <div>
          <div style={{ color: "#64748b" }}>解锁日期</div>
          <div style={{ color: "#e2e8f0", fontWeight: 600 }}>
            {e.unlock_date}
          </div>
          <div style={{ color: "#94a3b8", fontSize: 10 }}>
            {e.days_until_unlock >= 0
              ? `${e.days_until_unlock} 天后`
              : `${Math.abs(e.days_until_unlock)} 天前`}
          </div>
        </div>
        <div>
          <div style={{ color: "#64748b" }}>解锁量</div>
          <div style={{ color: "#e2e8f0", fontWeight: 600 }}>
            {formatTokens(e.unlock_tokens)}
          </div>
          <div style={{ color: "#94a3b8", fontSize: 10 }}>
            {formatUsd(e.unlock_usd)}
          </div>
        </div>
        <div>
          <div style={{ color: "#64748b" }}>占流通量</div>
          <div style={{ color: sc, fontWeight: 700, fontSize: 14 }}>
            {e.unlock_pct_circulating.toFixed(2)}%
          </div>
          <div style={{ color: "#94a3b8", fontSize: 10 }}>
            {categoryLabel(e.category)}
          </div>
        </div>
      </div>

      <PressureBar score={e.sell_pressure_score} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// 子组件：完整日历表格
// ---------------------------------------------------------------------------

function CalendarTable({ events }: { events: TokenUnlockEventData[] }) {
  if (events.length === 0) return null;

  return (
    <div
      style={{
        background: "#151619",
        border: "1px solid #2a2d35",
        borderRadius: 8,
        padding: "12px 14px",
        overflowX: "auto",
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
        完整解锁日历
      </div>

      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
        <thead>
          <tr style={{ color: "#64748b", borderBottom: "1px solid #2a2d35" }}>
            {["代币", "协议", "解锁日期", "距今", "占流通%", "类别", "抛压", "信号"].map(
              (h) => (
                <th
                  key={h}
                  style={{ textAlign: "left", padding: "4px 6px", fontWeight: 600 }}
                >
                  {h}
                </th>
              )
            )}
          </tr>
        </thead>
        <tbody>
          {events.map((e, i) => {
            const sc = signalColor(e.signal);
            return (
              <tr
                key={i}
                style={{
                  borderBottom: "1px solid #1a1d24",
                  color: "#e2e8f0",
                }}
              >
                <td style={{ padding: "5px 6px", fontWeight: 700 }}>{e.symbol}</td>
                <td style={{ padding: "5px 6px", color: "#94a3b8" }}>
                  {e.protocol.slice(0, 12)}
                </td>
                <td style={{ padding: "5px 6px" }}>{e.unlock_date}</td>
                <td
                  style={{
                    padding: "5px 6px",
                    color: e.days_until_unlock < 0 ? "#64748b" : "#e2e8f0",
                  }}
                >
                  {e.days_until_unlock >= 0
                    ? `+${e.days_until_unlock}d`
                    : `${e.days_until_unlock}d`}
                </td>
                <td
                  style={{
                    padding: "5px 6px",
                    color: e.unlock_pct_circulating >= 5 ? "#ef4444" : "#e2e8f0",
                    fontWeight: e.unlock_pct_circulating >= 5 ? 700 : 400,
                  }}
                >
                  {e.unlock_pct_circulating.toFixed(2)}%
                </td>
                <td style={{ padding: "5px 6px", color: "#94a3b8" }}>
                  {categoryLabel(e.category)}
                </td>
                <td style={{ padding: "5px 6px", minWidth: 70 }}>
                  <PressureBar score={e.sell_pressure_score} />
                </td>
                <td style={{ padding: "5px 6px" }}>
                  <span
                    style={{
                      fontSize: 10,
                      padding: "1px 6px",
                      borderRadius: 3,
                      background: sc + "22",
                      color: sc,
                    }}
                  >
                    {signalLabel(e.signal)}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

const DAYS_OPTIONS = [7, 14, 30, 60] as const;

export default function TokenUnlockPanel() {
  const [days, setDays] = useState<number>(30);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<TokenUnlockCalendarData | null>(null);

  async function load(d: number) {
    setLoading(true);
    setError(null);
    try {
      const result = await fetchTokenUnlocks(d);
      setData(result);
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load(days);
  }, [days]);

  const highRisk = data?.events.filter((e) => e.signal === "high_risk") ?? [];
  const allEvents = data?.events ?? [];

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
        <span>加密 Token 解锁日历</span>
        {data && (
          <span style={{ fontSize: 11, color: "#64748b", fontWeight: 400 }}>
            {data.total_events} 个事件
            {data.high_risk_count > 0 && (
              <span style={{ color: "#ef4444", marginLeft: 6 }}>
                ⚠ {data.high_risk_count} 高风险
              </span>
            )}
          </span>
        )}
      </div>

      {/* 时间范围选择 */}
      <div style={{ display: "flex", gap: 6, marginBottom: 14, alignItems: "center" }}>
        <span style={{ fontSize: 12, color: "#64748b" }}>展示范围：</span>
        {DAYS_OPTIONS.map((d) => (
          <button
            key={d}
            onClick={() => setDays(d)}
            style={{
              background: days === d ? "#00C087" : "#151619",
              color: days === d ? "#0E1014" : "#94a3b8",
              border: "1px solid " + (days === d ? "#00C087" : "#2a2d35"),
              borderRadius: 5,
              padding: "4px 12px",
              fontSize: 12,
              fontWeight: days === d ? 700 : 400,
              cursor: "pointer",
            }}
          >
            {d} 日
          </button>
        ))}
        {loading && (
          <span style={{ fontSize: 11, color: "#64748b", marginLeft: 4 }}>
            加载中...
          </span>
        )}
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

      {/* 高风险预警 */}
      {highRisk.length > 0 && (
        <div
          style={{
            background: "#151619",
            border: "1px solid #ef444433",
            borderRadius: 8,
            padding: "12px 14px",
            marginBottom: 10,
          }}
        >
          <div
            style={{
              fontSize: 12,
              fontWeight: 700,
              color: "#ef4444",
              marginBottom: 10,
              textTransform: "uppercase",
              letterSpacing: 1,
            }}
          >
            ⚠ 高风险解锁预警（抛压评分 ≥ 60%）
          </div>
          {highRisk.map((e, i) => (
            <UnlockCard key={i} e={e} />
          ))}
        </div>
      )}

      {/* 完整日历 */}
      {allEvents.length > 0 ? (
        <CalendarTable events={allEvents} />
      ) : (
        !loading &&
        data && (
          <div
            style={{
              textAlign: "center",
              color: "#475569",
              fontSize: 12,
              padding: "20px 0",
            }}
          >
            {data.total_events === 0
              ? "DefiLlama API 暂时无数据，请稍后重试"
              : "无解锁事件"}
          </div>
        )
      )}

      {data && (
        <div style={{ marginTop: 10, fontSize: 10, color: "#475569" }}>
          数据日期：{data.as_of_date}　│　来源：DefiLlama Emissions　│　不构成投资建议
        </div>
      )}
    </div>
  );
}

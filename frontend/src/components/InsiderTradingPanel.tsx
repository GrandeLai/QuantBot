/**
 * InsiderTradingPanel — SEC EDGAR Form 4 内部人交易聚类信号面板
 *
 * 功能：
 * - 集群信号 badge（cluster_buy / cluster_sell / mixed / neutral / no_data）
 * - 90 天内买入/卖出内部人计数
 * - 净持仓变动（总买入股份 - 总卖出股份）
 * - 最近交易明细表（内部人 / 职位 / 日期 / 数量 / 类型）
 * - 10b5-1 计划单已自动剔除
 *
 * Phase F.21 — Form 4 内部人交易聚类信号
 * 来源：SEC EDGAR data.sec.gov（免费）
 */
import { useState } from "react";
import {
  type InsiderSignal,
  type InsiderTradingData,
  fetchInsiderTrading,
} from "../api/client";

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function signalColor(signal: InsiderSignal): string {
  if (signal === "cluster_buy") return "#00C087";
  if (signal === "cluster_sell") return "#ef4444";
  if (signal === "mixed") return "#f59e0b";
  if (signal === "neutral") return "#64748b";
  return "#475569"; // no_data
}

function signalLabel(signal: InsiderSignal): string {
  const labels: Record<InsiderSignal, string> = {
    cluster_buy: "✓ 集群买入",
    cluster_sell: "✗ 集群卖出",
    mixed: "≈ 方向分歧",
    neutral: "中性",
    no_data: "无数据",
  };
  return labels[signal];
}

function fmtShares(n: number): string {
  if (Math.abs(n) >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (Math.abs(n) >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return n.toFixed(0);
}

// ---------------------------------------------------------------------------
// 子组件：交易明细行
// ---------------------------------------------------------------------------

function TxnRow({
  name,
  title,
  date,
  shares,
  price,
  txType,
}: {
  name: string;
  title: string;
  date: string;
  shares: number;
  price: number | null;
  txType: string;
}) {
  const isBuy = txType === "P";
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "2fr 1.2fr 1fr 1fr 0.6fr",
        gap: 4,
        padding: "4px 8px",
        borderBottom: "1px solid #1a1d24",
        fontSize: 11,
        alignItems: "center",
      }}
    >
      <div>
        <div style={{ color: "#e2e8f0", fontWeight: 600, fontSize: 11 }}>{name}</div>
        <div style={{ color: "#475569", fontSize: 9 }}>{title || "—"}</div>
      </div>
      <div style={{ color: "#94a3b8" }}>{date}</div>
      <div style={{ color: "#e2e8f0" }}>{fmtShares(shares)} 股</div>
      <div style={{ color: "#94a3b8" }}>{price != null ? `$${price.toFixed(2)}` : "—"}</div>
      <div
        style={{
          color: isBuy ? "#00C087" : "#ef4444",
          fontWeight: 700,
          textAlign: "right",
        }}
      >
        {isBuy ? "买入" : "卖出"}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export default function InsiderTradingPanel() {
  const [inputTicker, setInputTicker] = useState("AAPL");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<InsiderTradingData | null>(null);

  async function handleQuery() {
    if (!inputTicker.trim()) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const data = await fetchInsiderTrading(inputTicker.trim().toUpperCase());
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
        <span>内部人交易聚类信号 (Form 4)</span>
        <span style={{ fontSize: 10, color: "#475569", fontWeight: 400 }}>
          来源：SEC EDGAR · 90 天窗口 · 已剔除 10b5-1
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
              ⚠ SEC EDGAR 数据不可用（该股票可能不在 EDGAR 系统中，或接口暂时不可用）。
            </div>
          )}

          {/* 信号卡片 */}
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
                marginBottom: 10,
              }}
            >
              <span
                style={{
                  fontSize: 18,
                  fontWeight: 700,
                  color: signalColor(result.signal),
                  padding: "2px 12px",
                  borderRadius: 4,
                  background: signalColor(result.signal) + "22",
                }}
              >
                {signalLabel(result.signal)}
              </span>
              <div style={{ textAlign: "right" }}>
                <div style={{ fontSize: 11, color: "#94a3b8" }}>
                  净变动 {result.net_shares_90d >= 0 ? "+" : ""}
                  {fmtShares(result.net_shares_90d)} 股
                </div>
                <div style={{ fontSize: 10, color: "#475569" }}>90 天内</div>
              </div>
            </div>

            {/* 买入/卖出计数 */}
            <div style={{ display: "flex", gap: 8 }}>
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
                <div style={{ fontSize: 18, fontWeight: 700, color: "#00C087" }}>
                  {result.cluster_buy_count}
                </div>
                <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>内部人买入</div>
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
                <div style={{ fontSize: 18, fontWeight: 700, color: "#ef4444" }}>
                  {result.cluster_sell_count}
                </div>
                <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>内部人卖出</div>
              </div>
              {result.cik && (
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
                  <div style={{ fontSize: 11, fontWeight: 600, color: "#94a3b8" }}>
                    {result.cik}
                  </div>
                  <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>SEC CIK</div>
                </div>
              )}
            </div>
          </div>

          {/* 交易明细 */}
          {result.transactions.length > 0 && (
            <div
              style={{
                background: "#151619",
                border: "1px solid #2a2d35",
                borderRadius: 8,
                marginBottom: 10,
                overflow: "hidden",
              }}
            >
              {/* 表头 */}
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "2fr 1.2fr 1fr 1fr 0.6fr",
                  gap: 4,
                  padding: "5px 8px",
                  background: "#1a1d24",
                  fontSize: 9,
                  color: "#475569",
                  fontWeight: 600,
                  textTransform: "uppercase",
                }}
              >
                <div>内部人</div>
                <div>日期</div>
                <div>数量</div>
                <div>价格</div>
                <div style={{ textAlign: "right" }}>类型</div>
              </div>
              {result.transactions.slice(0, 10).map((t, i) => (
                <TxnRow
                  key={i}
                  name={t.insider_name}
                  title={t.title}
                  date={t.transaction_date}
                  shares={t.shares}
                  price={t.price_per_share}
                  txType={t.transaction_type}
                />
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
            <div>集群阈值：≥2 位内部人在 90 天内买入/卖出；10b5-1 自动计划单已剔除</div>
            <div>数据来源：SEC EDGAR data.sec.gov（免费，T+1 延迟）</div>
            <div>Cohen et al.（2012）：集群买入对应 180 日超额收益 6-10%</div>
          </div>

          <div style={{ marginTop: 8, fontSize: 10, color: "#475569" }}>
            数据日期：{result.as_of_date}　│　来源：SEC EDGAR　│　不构成投资建议
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
          输入股票代码查询 SEC Form 4 内部人交易聚类信号（90 天窗口）
        </div>
      )}
    </div>
  );
}

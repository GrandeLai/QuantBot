/**
 * IndexRebalancePanel — 指数成分股检测 + 调仓机会面板
 *
 * 功能：
 * - S&P 500 / NASDAQ 100 / Russell 2000 成分检查
 * - 市值显示 + 纳入/剔除风险评级
 * - 调仓机会标注（纳入前 1 月预期超额 +8%）
 *
 * Phase F.23 — Index Rebalance Preview
 * 来源：Wikipedia 成分股列表 + yfinance 市值
 */
import { useState } from "react";
import {
  type IndexMembershipItem,
  type IndexRebalanceData,
  type IndexStatus,
  type RebalanceRisk,
  fetchIndexRebalance,
} from "../api/client";

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function statusColor(status: IndexStatus): string {
  if (status === "member") return "#00C087";
  if (status === "non_member") return "#64748b";
  return "#475569";
}

function statusLabel(status: IndexStatus): string {
  if (status === "member") return "✓ 成分股";
  if (status === "non_member") return "非成分股";
  return "未知";
}

function riskColor(risk: RebalanceRisk): string {
  if (risk === "high_addition_risk") return "#00C087";
  if (risk === "moderate_addition_risk") return "#34d399";
  if (risk === "stable") return "#475569";
  if (risk === "moderate_deletion_risk") return "#f59e0b";
  if (risk === "high_deletion_risk") return "#ef4444";
  return "#475569";
}

function riskLabel(risk: RebalanceRisk): string {
  const labels: Record<RebalanceRisk, string> = {
    high_addition_risk: "★ 高概率纳入",
    moderate_addition_risk: "△ 接近纳入阈值",
    stable: "稳定",
    moderate_deletion_risk: "⚠ 接近剔除阈值",
    high_deletion_risk: "✗ 高风险剔除",
    unknown: "—",
  };
  return labels[risk];
}

function fmtCap(b: number | null): string {
  if (b == null) return "—";
  if (b >= 1000) return `$${(b / 1000).toFixed(1)}T`;
  return `$${b.toFixed(1)}B`;
}

// ---------------------------------------------------------------------------
// 子组件：指数行
// ---------------------------------------------------------------------------

function IndexRow({ idx }: { idx: IndexMembershipItem }) {
  const isOpportunity = idx.rebalance_risk === "high_addition_risk";

  return (
    <div
      style={{
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
        padding: "8px 10px",
        borderBottom: "1px solid #1a1d24",
        background: isOpportunity ? "#00C08708" : "transparent",
      }}
    >
      <div>
        <div style={{ fontSize: 12, fontWeight: 600, color: "#e2e8f0" }}>
          {idx.index_name}
        </div>
      </div>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <span
          style={{
            fontSize: 10,
            color: riskColor(idx.rebalance_risk),
            padding: "1px 6px",
            borderRadius: 3,
            background: riskColor(idx.rebalance_risk) + "22",
          }}
        >
          {riskLabel(idx.rebalance_risk)}
        </span>
        <span
          style={{
            fontSize: 11,
            fontWeight: 600,
            color: statusColor(idx.status),
          }}
        >
          {statusLabel(idx.status)}
        </span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export default function IndexRebalancePanel() {
  const [inputTicker, setInputTicker] = useState("AAPL");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<IndexRebalanceData | null>(null);

  async function handleQuery() {
    if (!inputTicker.trim()) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const data = await fetchIndexRebalance(inputTicker.trim().toUpperCase());
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

  // Count addition opportunities
  const opportunities = result?.indices.filter(
    (i) => i.rebalance_risk === "high_addition_risk" || i.rebalance_risk === "moderate_addition_risk"
  ).length ?? 0;

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
        <span>指数调仓机会面板</span>
        <span style={{ fontSize: 10, color: "#475569", fontWeight: 400 }}>
          S&P 500 · NASDAQ 100 · Russell 2000
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
              ⚠ 市值数据不可用（yfinance 暂时无法获取）。
            </div>
          )}

          {/* 市值信息卡 */}
          <div
            style={{
              background: "#151619",
              border: "1px solid #2a2d35",
              borderRadius: 8,
              padding: "10px 14px",
              marginBottom: 10,
              display: "flex",
              gap: 8,
            }}
          >
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
              <div style={{ fontSize: 15, fontWeight: 700, color: "#e2e8f0" }}>
                {fmtCap(result.market_cap_b)}
              </div>
              <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>市值</div>
            </div>
            {result.price != null && (
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
                <div style={{ fontSize: 15, fontWeight: 700, color: "#e2e8f0" }}>
                  ${result.price.toFixed(2)}
                </div>
                <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>当前价</div>
              </div>
            )}
            {result.eps_ttm != null && (
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
                    color: result.eps_ttm > 0 ? "#00C087" : "#ef4444",
                  }}
                >
                  ${result.eps_ttm.toFixed(2)}
                </div>
                <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>EPS TTM</div>
              </div>
            )}
            {opportunities > 0 && (
              <div
                style={{
                  flex: 1,
                  background: "#00C08711",
                  border: "1px solid #00C08733",
                  borderRadius: 5,
                  padding: "6px 8px",
                  textAlign: "center",
                }}
              >
                <div style={{ fontSize: 15, fontWeight: 700, color: "#00C087" }}>
                  {opportunities}
                </div>
                <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>调仓机会</div>
              </div>
            )}
          </div>

          {/* 指数成分表 */}
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
              <span>指数</span>
              <span>调仓风险 · 状态</span>
            </div>
            {result.indices.map((idx, i) => (
              <IndexRow key={i} idx={idx} />
            ))}
          </div>

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
            <div>S&P 500 纳入前 1 个月平均超额 +8%（Chen, Noronha & Singal 2004）</div>
            <div>S&P 500 最低市值门槛约 $14.5B；NASDAQ 100 约 $5B（动态调整）</div>
            <div>Russell 2000 为年度重组（6 月），以市值范围估算</div>
          </div>

          <div style={{ marginTop: 8, fontSize: 10, color: "#475569" }}>
            数据日期：{result.as_of_date}　│　来源：Wikipedia + yfinance　│　不构成投资建议
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
          输入股票代码查询指数成分股状态与调仓机会
        </div>
      )}
    </div>
  );
}

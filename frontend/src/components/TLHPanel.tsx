/**
 * TLHPanel — 税务亏损收割（Tax-Loss Harvesting）面板
 *
 * 功能：
 * - 接受用户输入的持仓 lots（ticker、数量、成本、买入日期）
 * - 调用 POST /api/tlh/scan 获取候选列表
 * - 展示候选、预估节税、替代 ETF、wash sale 风险提示
 *
 * 免责声明：本工具仅供参考，不构成税务建议，请咨询 CPA。
 *
 * Phase F.3 — #0E1014 / #151619 / #00C087 dark-theme tokens
 */
import React, { useState } from "react";
import { type TaxLotInput, type TLHCandidate, fetchTLHScan } from "../api/client";

// ---------------------------------------------------------------------------
// 类型
// ---------------------------------------------------------------------------

interface LotRow {
  id: string;
  ticker: string;
  quantity: string;
  cost_basis: string;
  acquisition_date: string;
  current_price: string;
}

// ---------------------------------------------------------------------------
// 内部子组件
// ---------------------------------------------------------------------------

function Disclaimer() {
  return (
    <div
      style={{
        background: "#1a1c22",
        border: "1px solid #f59e0b44",
        borderRadius: 6,
        padding: "8px 12px",
        marginBottom: 12,
        display: "flex",
        alignItems: "flex-start",
        gap: 8,
        fontSize: 12,
        color: "#f59e0b",
      }}
    >
      <span>⚠</span>
      <span>
        <strong>免责声明：</strong>本工具仅供参考，不构成税务建议（disclaimer），请务必咨询
        CPA 或税务顾问，确认您的具体税务处理方式。不构成税务建议，用户自行承担决策风险。
      </span>
    </div>
  );
}

interface CandidateCardProps {
  candidate: TLHCandidate;
}

function CandidateCard({ candidate: c }: CandidateCardProps) {
  const [expanded, setExpanded] = useState(false);
  const pnlColor = c.unrealized_pnl < 0 ? "#ef4444" : "#00C087";
  const pctStr = (c.unrealized_pnl_pct * 100).toFixed(2);
  const termLabel = c.is_long_term ? "长期" : "短期";
  const termColor = c.is_long_term ? "#00C087" : "#f59e0b";

  return (
    <div
      style={{
        background: "#151619",
        border: "1px solid #2a2d35",
        borderRadius: 8,
        padding: "12px 14px",
        marginBottom: 8,
      }}
    >
      {/* 头部行 */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ fontWeight: 700, fontSize: 15, color: "#e2e8f0" }}>
            {c.ticker}
          </span>
          <span style={{ fontSize: 12, color: "#94a3b8" }}>
            × {c.quantity.toLocaleString()} 股
          </span>
          <span
            style={{
              fontSize: 11,
              padding: "2px 6px",
              borderRadius: 4,
              background: termColor + "22",
              color: termColor,
            }}
          >
            {termLabel}（{c.holding_days} 天）
          </span>
        </div>
        <div style={{ textAlign: "right" }}>
          <span style={{ fontWeight: 600, fontSize: 14, color: pnlColor }}>
            {c.unrealized_pnl < 0 ? "-" : ""}$
            {Math.abs(c.unrealized_pnl).toLocaleString(undefined, { maximumFractionDigits: 0 })}
          </span>
          <span style={{ fontSize: 12, color: pnlColor, marginLeft: 6 }}>
            ({pctStr}%)
          </span>
        </div>
      </div>

      {/* 替代 ETF */}
      {c.replacement_tickers.length > 0 && (
        <div style={{ marginTop: 8, fontSize: 12, color: "#94a3b8" }}>
          <span>替代：</span>
          {c.replacement_tickers.map((t) => (
            <span
              key={t}
              style={{
                display: "inline-block",
                marginLeft: 6,
                padding: "2px 7px",
                background: "#00C08722",
                color: "#00C087",
                borderRadius: 4,
                fontWeight: 600,
              }}
            >
              {t}
            </span>
          ))}
        </div>
      )}

      {/* Wash Sale 警告 */}
      {c.wash_sale_risk && (
        <div
          style={{
            marginTop: 8,
            padding: "5px 10px",
            background: "#ef444422",
            border: "1px solid #ef444444",
            borderRadius: 5,
            fontSize: 12,
            color: "#ef4444",
            display: "flex",
            gap: 6,
          }}
        >
          <span>⚠</span>
          <span>Wash Sale 风险：近 30 天内有买入记录，卖出后须等 30 天再回购</span>
        </div>
      )}

      {/* 展开详情 */}
      <button
        onClick={() => setExpanded(!expanded)}
        style={{
          marginTop: 8,
          fontSize: 11,
          color: "#64748b",
          background: "none",
          border: "none",
          cursor: "pointer",
          padding: 0,
        }}
      >
        {expanded ? "▲ 收起" : "▼ 展开详情"}
      </button>
      {expanded && (
        <div
          style={{
            marginTop: 8,
            fontSize: 12,
            color: "#94a3b8",
            borderTop: "1px solid #2a2d35",
            paddingTop: 8,
            lineHeight: 1.8,
          }}
        >
          <div>当前价格：${c.current_price.toFixed(2)}</div>
          <div>
            成本基础：${c.cost_basis.toFixed(2)} × {c.quantity} = $
            {(c.cost_basis * c.quantity).toLocaleString(undefined, { maximumFractionDigits: 0 })}
          </div>
          <div>买入日期：{c.acquisition_date}</div>
          <div>Lot ID：{c.lot_id || "—"}</div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export default function TLHPanel() {
  const [rows, setRows] = useState<LotRow[]>([
    {
      id: "1",
      ticker: "AAPL",
      quantity: "100",
      cost_basis: "150",
      acquisition_date: "2024-06-01",
      current_price: "130",
    },
  ]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{
    candidates: TLHCandidate[];
    estimated_tax_saving: number;
  } | null>(null);

  // 短期/长期税率（用户可调）
  const [shortRate, setShortRate] = useState(37);
  const [longRate, setLongRate] = useState(20);

  function addRow() {
    setRows((prev) => [
      ...prev,
      {
        id: String(Date.now()),
        ticker: "",
        quantity: "",
        cost_basis: "",
        acquisition_date: "",
        current_price: "",
      },
    ]);
  }

  function removeRow(id: string) {
    setRows((prev) => prev.filter((r) => r.id !== id));
  }

  function updateRow(id: string, field: keyof LotRow, value: string) {
    setRows((prev) => prev.map((r) => (r.id === id ? { ...r, [field]: value } : r)));
  }

  async function handleScan() {
    setError(null);
    setResult(null);

    // 验证并构建 lots + prices
    const lots: TaxLotInput[] = [];
    const prices: Record<string, number> = {};
    for (const row of rows) {
      if (!row.ticker || !row.quantity || !row.cost_basis || !row.acquisition_date) continue;
      lots.push({
        ticker: row.ticker.toUpperCase(),
        quantity: parseFloat(row.quantity),
        cost_basis: parseFloat(row.cost_basis),
        acquisition_date: row.acquisition_date,
        lot_id: row.id,
      });
      if (row.current_price) {
        prices[row.ticker.toUpperCase()] = parseFloat(row.current_price);
      }
    }

    if (lots.length === 0) {
      setError("请至少填写一行持仓信息");
      return;
    }

    setLoading(true);
    try {
      const resp = await fetchTLHScan(lots, prices);
      // 按用户填写的税率重新估算（前端估算，直接用响应中的候选）
      const stRate = shortRate / 100;
      const ltRate = longRate / 100;
      const recalcSaving = resp.candidates.reduce((sum, c) => {
        return sum + Math.abs(c.unrealized_pnl) * (c.is_long_term ? ltRate : stRate);
      }, 0);
      setResult({
        candidates: resp.candidates,
        estimated_tax_saving: Math.round(recalcSaving * 100) / 100,
      });
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
    padding: "4px 8px",
    fontSize: 12,
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
          marginBottom: 10,
          borderBottom: "1px solid #2a2d35",
          paddingBottom: 8,
        }}
      >
        税务亏损收割（TLH）
      </div>

      {/* 免责声明 */}
      <Disclaimer />

      {/* 税率配置 */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 16,
          marginBottom: 12,
          fontSize: 12,
          color: "#94a3b8",
        }}
      >
        <span style={{ fontWeight: 600, color: "#e2e8f0" }}>税率配置：</span>
        <label style={{ display: "flex", alignItems: "center", gap: 4 }}>
          短期税率
          <input
            type="number"
            value={shortRate}
            onChange={(e) => setShortRate(Number(e.target.value))}
            style={{ ...inputStyle, width: 55, textAlign: "center" }}
            min={0}
            max={100}
          />
          %
        </label>
        <label style={{ display: "flex", alignItems: "center", gap: 4 }}>
          长期税率
          <input
            type="number"
            value={longRate}
            onChange={(e) => setLongRate(Number(e.target.value))}
            style={{ ...inputStyle, width: 55, textAlign: "center" }}
            min={0}
            max={100}
          />
          %
        </label>
      </div>

      {/* 持仓输入表格 */}
      <div
        style={{
          background: "#151619",
          border: "1px solid #2a2d35",
          borderRadius: 8,
          padding: 12,
          marginBottom: 12,
        }}
      >
        <div style={{ fontSize: 12, fontWeight: 600, color: "#94a3b8", marginBottom: 8 }}>
          输入持仓 Lots
        </div>
        {/* 表头 */}
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "80px 70px 80px 110px 90px 32px",
            gap: 6,
            fontSize: 11,
            color: "#64748b",
            marginBottom: 6,
          }}
        >
          <span>代码</span>
          <span>数量</span>
          <span>成本($/股)</span>
          <span>买入日期</span>
          <span>当前价($)</span>
          <span></span>
        </div>
        {rows.map((row) => (
          <div
            key={row.id}
            style={{
              display: "grid",
              gridTemplateColumns: "80px 70px 80px 110px 90px 32px",
              gap: 6,
              marginBottom: 5,
              alignItems: "center",
            }}
          >
            <input
              value={row.ticker}
              onChange={(e) => updateRow(row.id, "ticker", e.target.value.toUpperCase())}
              placeholder="AAPL"
              style={{ ...inputStyle, width: "100%" }}
            />
            <input
              value={row.quantity}
              onChange={(e) => updateRow(row.id, "quantity", e.target.value)}
              placeholder="100"
              type="number"
              style={{ ...inputStyle, width: "100%" }}
            />
            <input
              value={row.cost_basis}
              onChange={(e) => updateRow(row.id, "cost_basis", e.target.value)}
              placeholder="150.00"
              type="number"
              step="0.01"
              style={{ ...inputStyle, width: "100%" }}
            />
            <input
              value={row.acquisition_date}
              onChange={(e) => updateRow(row.id, "acquisition_date", e.target.value)}
              placeholder="YYYY-MM-DD"
              style={{ ...inputStyle, width: "100%" }}
            />
            <input
              value={row.current_price}
              onChange={(e) => updateRow(row.id, "current_price", e.target.value)}
              placeholder="130.00"
              type="number"
              step="0.01"
              style={{ ...inputStyle, width: "100%" }}
            />
            <button
              onClick={() => removeRow(row.id)}
              style={{
                background: "none",
                border: "none",
                color: "#ef4444",
                cursor: "pointer",
                fontSize: 14,
                lineHeight: 1,
              }}
            >
              ✕
            </button>
          </div>
        ))}
        <button
          onClick={addRow}
          style={{
            marginTop: 6,
            fontSize: 11,
            color: "#00C087",
            background: "none",
            border: "1px dashed #00C08744",
            borderRadius: 4,
            padding: "3px 10px",
            cursor: "pointer",
          }}
        >
          + 添加 Lot
        </button>
      </div>

      {/* 扫描按钮 */}
      <button
        onClick={handleScan}
        disabled={loading}
        style={{
          background: loading ? "#1f2937" : "#00C087",
          color: loading ? "#94a3b8" : "#0E1014",
          border: "none",
          borderRadius: 6,
          padding: "8px 20px",
          fontWeight: 700,
          fontSize: 13,
          cursor: loading ? "not-allowed" : "pointer",
          marginBottom: 12,
        }}
      >
        {loading ? "扫描中..." : "扫描"}
      </button>

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

      {/* 结果区 */}
      {result && (
        <>
          {/* 预计节税 */}
          <div
            style={{
              background: "#151619",
              border: "1px solid #00C08744",
              borderRadius: 8,
              padding: "10px 14px",
              marginBottom: 12,
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
            }}
          >
            <span style={{ fontSize: 13, color: "#94a3b8" }}>预计节税</span>
            <span style={{ fontSize: 18, fontWeight: 700, color: "#00C087" }}>
              ${result.estimated_tax_saving.toLocaleString(undefined, { maximumFractionDigits: 0 })}
            </span>
          </div>

          {/* 候选仓位 */}
          {result.candidates.length === 0 ? (
            <div style={{ fontSize: 12, color: "#64748b", textAlign: "center", padding: 16 }}>
              暂无满足条件的候选仓位（跌幅 &lt; 5% 或亏损 &lt; $500）
            </div>
          ) : (
            <>
              <div
                style={{
                  fontSize: 12,
                  fontWeight: 600,
                  color: "#94a3b8",
                  marginBottom: 8,
                  borderTop: "1px solid #2a2d35",
                  paddingTop: 12,
                }}
              >
                候选仓位 ({result.candidates.length})
              </div>
              {result.candidates.map((c) => (
                <CandidateCard key={`${c.ticker}-${c.lot_id}`} candidate={c} />
              ))}
            </>
          )}

          {/* 合规提示 */}
          <div
            style={{
              background: "#151619",
              border: "1px solid #2a2d35",
              borderRadius: 8,
              padding: "10px 14px",
              marginTop: 12,
              fontSize: 11,
              color: "#64748b",
              lineHeight: 1.8,
            }}
          >
            <div style={{ fontWeight: 600, color: "#94a3b8", marginBottom: 4 }}>合规提示</div>
            <div>• 卖出后 30 天内不得回购同一或 substantially identical 证券（IRS Wash Sale Rule §1091）</div>
            <div>• 推荐先买替代 ETF，待 30 天后再重新建仓原股</div>
            <div>• 替代 ETF 相关性高但发行商不同，暂无 IRS substantially identical 认定风险（请 CPA 确认）</div>
            <div>• 本工具不构成税务建议，节税金额为估算，实际税务处理请咨询 CPA</div>
          </div>
        </>
      )}
    </div>
  );
}

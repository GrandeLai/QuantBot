/**
 * UnusualOptionsPanel — 期权异常活动扫描器
 *
 * 功能：
 * - 扫描近期到期期权的 Volume/OI 比，识别机构预布局信号
 * - Put/Call 比例条 + 异常合约列表
 * - grade badge（看涨异动 / 看跌异动 / 双向 / 正常）
 * - 数据不可用降级提示
 *
 * Phase F.16 — 期权微观结构 alpha
 * 参考：Easley, O'Hara & Srinivas (1998), Barchart/Unusual Whales 方法论
 */
import React, { useState } from "react";
import {
  type OptionsGrade,
  type UnusualContractData,
  type UnusualOptionsData,
  fetchUnusualOptions,
} from "../api/client";

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function gradeColor(grade: OptionsGrade): string {
  if (grade === "bullish_unusual") return "#00C087";
  if (grade === "bearish_unusual") return "#ef4444";
  if (grade === "mixed_unusual") return "#f59e0b";
  return "#64748b";
}

function gradeLabel(grade: OptionsGrade): string {
  const labels: Record<OptionsGrade, string> = {
    bullish_unusual: "看涨异动 ⬆",
    bearish_unusual: "看跌异动 ⬇",
    mixed_unusual: "双向异动 ↔",
    neutral: "正常",
  };
  return labels[grade];
}

function fmtVol(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
  return String(n);
}

// ---------------------------------------------------------------------------
// 子组件：Put/Call 比例条
// ---------------------------------------------------------------------------

function PutCallBar({ data }: { data: UnusualOptionsData }) {
  const total = data.total_call_volume + data.total_put_volume;
  const callPct = total > 0 ? (data.total_call_volume / total) * 100 : 50;
  const putPct = 100 - callPct;

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
          Call / Put 成交量比
        </div>
        <span style={{ fontSize: 12, color: "#e2e8f0" }}>
          P/C 比 {data.put_call_ratio.toFixed(2)}
        </span>
      </div>

      {/* Ratio bar */}
      <div
        style={{
          display: "flex",
          height: 10,
          borderRadius: 5,
          overflow: "hidden",
          marginBottom: 6,
        }}
      >
        <div
          style={{
            width: `${callPct}%`,
            background: "#00C087",
            transition: "width 0.4s ease",
          }}
        />
        <div
          style={{
            width: `${putPct}%`,
            background: "#ef4444",
            transition: "width 0.4s ease",
          }}
        />
      </div>

      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: "#94a3b8" }}>
        <span style={{ color: "#00C087", fontWeight: 700 }}>
          Call {fmtVol(data.total_call_volume)} ({callPct.toFixed(0)}%)
        </span>
        <span style={{ color: "#ef4444", fontWeight: 700 }}>
          Put {fmtVol(data.total_put_volume)} ({putPct.toFixed(0)}%)
        </span>
      </div>

      <div style={{ marginTop: 6, fontSize: 9, color: "#475569" }}>
        异常合约：{data.total_unusual_calls} call + {data.total_unusual_puts} put（Volume/OI {'>'} 3×）
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 子组件：单个异常合约行
// ---------------------------------------------------------------------------

function ContractRow({ c }: { c: UnusualContractData }) {
  const isCall = c.option_type === "call";
  const typeColor = isCall ? "#00C087" : "#ef4444";
  const ratioColor = c.volume_oi_ratio >= 10 ? "#f59e0b" : "#e2e8f0";

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "48px 70px 65px 65px 75px 55px",
        gap: 4,
        padding: "5px 8px",
        borderBottom: "1px solid #1a1d24",
        alignItems: "center",
        fontSize: 11,
      }}
    >
      <span
        style={{
          color: typeColor,
          fontWeight: 700,
          textTransform: "uppercase",
          fontSize: 10,
        }}
      >
        {c.option_type}
      </span>
      <span style={{ color: "#94a3b8" }}>{c.expiry.slice(5)}</span>
      <span style={{ color: "#e2e8f0" }}>${c.strike.toFixed(0)}</span>
      <span style={{ color: "#94a3b8" }}>{fmtVol(c.volume)}</span>
      <span style={{ color: ratioColor, fontWeight: 700 }}>
        {c.volume_oi_ratio.toFixed(1)}× OI
      </span>
      <span style={{ color: "#475569" }}>{(c.implied_volatility * 100).toFixed(0)}% IV</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export default function UnusualOptionsPanel() {
  const [inputTicker, setInputTicker] = useState("AAPL");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<UnusualOptionsData | null>(null);

  async function handleQuery() {
    if (!inputTicker.trim()) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const data = await fetchUnusualOptions(inputTicker.trim().toUpperCase());
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
        <span>期权异常活动（UOA）</span>
        <span style={{ fontSize: 10, color: "#475569", fontWeight: 400 }}>
          来源：yfinance · Volume/OI {'>'} 3×
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
          {loading ? "扫描中..." : "扫描"}
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
          {/* 数据不可用降级提示 */}
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
              ⚠ 期权链数据暂时不可用（可能无期权或 yfinance 超时）。
            </div>
          )}

          {/* Grade badge + P/C ratio */}
          <div
            style={{
              background: "#151619",
              border: "1px solid #2a2d35",
              borderRadius: 8,
              padding: "10px 14px",
              marginBottom: 10,
              display: "flex",
              alignItems: "center",
              gap: 16,
            }}
          >
            <span
              style={{
                fontSize: 22,
                fontWeight: 700,
                color: gradeColor(result.grade),
              }}
            >
              {gradeLabel(result.grade)}
            </span>
            <div style={{ fontSize: 10, color: "#475569" }}>
              <div>看涨异动：{result.total_unusual_calls} 合约</div>
              <div>看跌异动：{result.total_unusual_puts} 合约</div>
            </div>
          </div>

          {/* Put/Call bar */}
          <PutCallBar data={result} />

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

          {/* Top unusual contracts */}
          {result.top_unusual.length > 0 && (
            <div
              style={{
                background: "#151619",
                border: "1px solid #2a2d35",
                borderRadius: 8,
                overflow: "hidden",
                marginBottom: 10,
              }}
            >
              {/* Header */}
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "48px 70px 65px 65px 75px 55px",
                  gap: 4,
                  padding: "6px 8px",
                  borderBottom: "1px solid #2a2d35",
                  fontSize: 9,
                  fontWeight: 700,
                  color: "#475569",
                  textTransform: "uppercase",
                }}
              >
                <span>类型</span>
                <span>到期日</span>
                <span>行权价</span>
                <span>成交量</span>
                <span>Vol/OI</span>
                <span>IV</span>
              </div>
              {result.top_unusual.map((c, i) => (
                <ContractRow key={i} c={c} />
              ))}
            </div>
          )}

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
            <div>Vol/OI {'>'} 3× = 当日成交量超过未平仓量 3 倍，暗示非常规方向性押注</div>
            <div>扫描最近 3 个到期日 · 参考：Easley, O'Hara &amp; Srinivas (1998)</div>
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
          输入股票代码扫描期权异常活动（Volume/OI &gt; 3×）
        </div>
      )}
    </div>
  );
}

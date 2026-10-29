/**
 * MacroDashboardPanel — 宏观仪表盘
 *
 * 功能（自动加载，无需用户输入）：
 * - VIX 恐慌指数 + 52 周百分位
 * - 收益率曲线（10年-3月利差）+ 反转预警
 * - 美元指数（DXY）
 * - 黄金与原油价格
 * - 综合宏观情绪分级
 *
 * Phase F.27 — Macro Dashboard
 * 数据来源：yfinance 免费宏观符号（^VIX, ^TNX, ^IRX, DX-Y.NYB, GC=F, CL=F）
 */
import { useEffect, useState } from "react";
import {
  type MacroDashboardData,
  type MacroRegime,
  fetchMacroDashboard,
} from "../api/client";

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function regimeColor(r: MacroRegime): string {
  if (r === "risk_on") return "#00C087";
  if (r === "neutral") return "#94a3b8";
  if (r === "risk_off") return "#f59e0b";
  if (r === "extreme_risk_off") return "#ef4444";
  return "#475569";
}

function regimeLabel(r: MacroRegime): string {
  const labels: Record<MacroRegime, string> = {
    risk_on: "✓ Risk-On（加仓）",
    neutral: "→ 中性",
    risk_off: "⚠ Risk-Off（减仓）",
    extreme_risk_off: "🔴 极度避险（大幅减仓/对冲）",
    unknown: "—",
  };
  return labels[r];
}

function fmt(v: number | null, decimals = 2, prefix = ""): string {
  if (v == null) return "—";
  return `${prefix}${v.toFixed(decimals)}`;
}

function fmtSpread(v: number | null): string {
  if (v == null) return "—";
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}pp`;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function MacroCard({
  label,
  value,
  sub,
  color,
  alert,
}: {
  label: string;
  value: string;
  sub?: string;
  color?: string;
  alert?: boolean;
}) {
  return (
    <div
      style={{
        flex: 1,
        background: alert ? "#ef444411" : "#0E1014",
        border: `1px solid ${alert ? "#ef444455" : "#2a2d35"}`,
        borderRadius: 5,
        padding: "7px 8px",
        textAlign: "center",
        minWidth: 70,
      }}
    >
      <div
        style={{
          fontSize: 14,
          fontWeight: 700,
          color: color ?? "#e2e8f0",
        }}
      >
        {value}
      </div>
      {sub && (
        <div style={{ fontSize: 8, color: "#64748b", marginTop: 1 }}>
          {sub}
        </div>
      )}
      <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>{label}</div>
    </div>
  );
}

function VixBar({ vix, pct }: { vix: number | null; pct: number | null }) {
  if (vix == null) return null;
  const normalized = Math.min(100, (vix / 50) * 100); // cap at VIX=50
  const color = vix >= 30 ? "#ef4444" : vix >= 20 ? "#f59e0b" : "#00C087";

  return (
    <div style={{ margin: "6px 0" }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 3 }}>
        <span style={{ fontSize: 10, color: "#475569" }}>VIX 0</span>
        <span style={{ fontSize: 12, fontWeight: 700, color }}>
          VIX {vix.toFixed(1)}
          {pct != null ? ` (${pct.toFixed(0)}th pct)` : ""}
        </span>
        <span style={{ fontSize: 10, color: "#ef4444" }}>VIX 50+</span>
      </div>
      <div style={{ background: "#1a1d24", borderRadius: 4, height: 7, overflow: "hidden" }}>
        {/* zone coloring */}
        <div
          style={{
            position: "relative",
            height: "100%",
            background: `linear-gradient(to right, #00C087 0%, #00C087 30%, #f59e0b 30%, #f59e0b 50%, #ef4444 50%)`,
            opacity: 0.2,
          }}
        />
        <div
          style={{
            position: "relative",
            marginTop: -7,
            height: "100%",
            width: `${normalized}%`,
            background: color,
            borderRadius: 4,
            transition: "width 0.4s",
          }}
        />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export default function MacroDashboardPanel() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<MacroDashboardData | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchMacroDashboard();
      setResult(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  // Auto-load on mount
  useEffect(() => {
    load();
  }, []);

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
        <span>宏观仪表盘</span>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span style={{ fontSize: 10, color: "#475569", fontWeight: 400 }}>
            VIX · 收益率曲线 · DXY · 黄金 · 原油
          </span>
          <button
            onClick={load}
            disabled={loading}
            style={{
              background: "transparent",
              border: "1px solid #2a2d35",
              borderRadius: 4,
              color: "#64748b",
              padding: "2px 8px",
              fontSize: 10,
              cursor: loading ? "not-allowed" : "pointer",
            }}
          >
            {loading ? "刷新中..." : "↺ 刷新"}
          </button>
        </div>
      </div>

      {/* 加载 */}
      {loading && !result && (
        <div style={{ color: "#475569", fontSize: 12, textAlign: "center", padding: "20px 0" }}>
          正在获取宏观数据...
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
              ⚠ 宏观数据不可用（yfinance 暂时无法获取）。
            </div>
          )}

          {/* 情绪旗帜 */}
          {result.regime !== "unknown" && (
            <div style={{ marginBottom: 12 }}>
              <span
                style={{
                  display: "inline-block",
                  background: regimeColor(result.regime) + "22",
                  border: `1px solid ${regimeColor(result.regime)}55`,
                  color: regimeColor(result.regime),
                  borderRadius: 5,
                  padding: "3px 10px",
                  fontSize: 12,
                  fontWeight: 700,
                }}
              >
                {regimeLabel(result.regime)}
              </span>
            </div>
          )}

          {/* VIX 仪表盘 */}
          {result.vix != null && (
            <div
              style={{
                background: "#151619",
                border: "1px solid #2a2d35",
                borderRadius: 8,
                padding: "10px 14px",
                marginBottom: 10,
              }}
            >
              <VixBar vix={result.vix} pct={result.vix_pct_52w} />
            </div>
          )}

          {/* 宏观指标卡片行 */}
          <div
            style={{
              background: "#151619",
              border: "1px solid #2a2d35",
              borderRadius: 8,
              padding: "10px 14px",
              marginBottom: 10,
            }}
          >
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              {/* 收益率曲线 */}
              <MacroCard
                label="利差(10Y-3M)"
                value={fmtSpread(result.yield_spread)}
                sub={result.yield_curve_inverted ? "⚠ 倒挂" : "正常"}
                color={result.yield_curve_inverted ? "#ef4444" : "#00C087"}
                alert={result.yield_curve_inverted}
              />
              <MacroCard
                label="10Y 国债"
                value={fmt(result.yield_10y) + "%"}
                color="#e2e8f0"
              />
              <MacroCard
                label="3M 国债"
                value={fmt(result.yield_3m) + "%"}
                color="#e2e8f0"
              />
              {result.dxy != null && (
                <MacroCard
                  label="DXY"
                  value={result.dxy.toFixed(1)}
                  color="#e2e8f0"
                />
              )}
              {result.gold != null && (
                <MacroCard
                  label="黄金"
                  value={`$${result.gold.toFixed(0)}`}
                  color="#f59e0b"
                />
              )}
              {result.oil != null && (
                <MacroCard
                  label="WTI 原油"
                  value={`$${result.oil.toFixed(1)}`}
                  color="#64748b"
                />
              )}
            </div>
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
            <div>VIX ≥ 30 或曲线倒挂 + VIX 高百分位 → 极度避险模式</div>
            <div>10Y-3M 利差倒挂是历史上最可靠的衰退领先指标（12-24 个月领先）</div>
            <div>数据来源：^VIX · ^TNX · ^IRX · DX-Y.NYB · GC=F · CL=F</div>
          </div>

          <div style={{ marginTop: 8, fontSize: 10, color: "#475569" }}>
            数据日期：{result.as_of_date}　│　来源：yfinance　│　不构成投资建议
          </div>
        </div>
      )}
    </div>
  );
}

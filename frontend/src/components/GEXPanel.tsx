/**
 * GEX Panel — Dealer Gamma Exposure 仪表盘 (phaseF.options-gex-panel)
 *
 * 展示：
 *  - 关键水位：Gamma Flip / Major Magnet / High Vol Trigger / Net GEX
 *  - 各行权价 GEX 分布（BarChart：正值绿/负值红，竖线标注当前价）
 *  - 交易解读提示
 *
 * 数据来源：GET /api/options/gex/snapshot?ticker=...
 */
import { useCallback, useState } from "react";
import { Activity, RefreshCw, TrendingDown, TrendingUp, Zap } from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { cn } from "../lib/utils";
import {
  fetchGEXSnapshot,
  type GEXByStrike,
  type GEXSnapshot,
} from "../api/client";

// ── constants ─────────────────────────────────────────────────────────────────

type Ticker = "SPY" | "QQQ" | "IWM" | "TSLA" | "AAPL" | "NVDA";
const TICKERS: Ticker[] = ["SPY", "QQQ", "IWM", "TSLA", "AAPL", "NVDA"];

const DTE_OPTIONS = [7, 14, 21, 30, 45];

// ── helpers ───────────────────────────────────────────────────────────────────

function fmtDollar(n: number): string {
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs >= 1e9) return `${sign}$${(abs / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${sign}$${(abs / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${sign}$${(abs / 1e3).toFixed(1)}K`;
  return `${sign}$${abs.toFixed(0)}`;
}

function fmtStrike(n: number): string {
  return n >= 1000 ? n.toFixed(0) : n.toFixed(1);
}

// ── sub-components ────────────────────────────────────────────────────────────

function LevelBadge({
  label,
  value,
  color = "text-[#00C087]",
}: {
  label: string;
  value: string;
  color?: string;
}) {
  return (
    <div className="bg-[#0E1014] border border-[#2A2D35] rounded-lg px-3 py-2 min-w-[120px]">
      <div className="text-[#8E9299] text-[10px] font-bold uppercase tracking-wider">{label}</div>
      <div className={cn("text-lg font-bold mt-0.5", color)}>{value}</div>
    </div>
  );
}

interface TooltipPayloadEntry {
  name: string;
  value: number;
  payload: GEXByStrike & { gex_fmt: string };
}

function GEXTooltip({
  active,
  payload,
}: {
  active?: boolean;
  payload?: TooltipPayloadEntry[];
}) {
  if (!active || !payload || payload.length === 0) return null;
  const d = payload[0].payload;
  return (
    <div className="bg-[#1A1D22] border border-[#2A2D35] rounded-lg p-3 text-xs space-y-1 shadow-xl">
      <div className="text-white font-bold">Strike ${fmtStrike(d.strike)}</div>
      <div className="text-[#8E9299]">
        Net GEX:{" "}
        <span className={d.net_gex >= 0 ? "text-[#00C087]" : "text-red-400"}>
          {fmtDollar(d.net_gex)}
        </span>
      </div>
      <div className="text-[#8E9299]">
        Call OI: <span className="text-white">{d.call_oi.toLocaleString()}</span>
      </div>
      <div className="text-[#8E9299]">
        Put OI: <span className="text-white">{d.put_oi.toLocaleString()}</span>
      </div>
      <div className="text-[#8E9299]">
        Gamma: <span className="text-white">{d.gamma.toFixed(6)}</span>
      </div>
      <div className="text-[#8E9299]">
        DTE: <span className="text-white">{d.dte.toFixed(0)}</span>
      </div>
    </div>
  );
}

// ── main component ────────────────────────────────────────────────────────────

export default function GEXPanel() {
  const [ticker, setTicker] = useState<Ticker>("SPY");
  const [maxDte, setMaxDte] = useState(45);
  const [snapshot, setSnapshot] = useState<GEXSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchGEXSnapshot(ticker, maxDte);
      setSnapshot(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [ticker, maxDte]);

  // Chart data — thin out if too many strikes for readability
  const chartData = snapshot?.gex_by_strike ?? [];

  const isPositive = (snapshot?.net_gex_total ?? 0) >= 0;

  return (
    <section className="bg-[#151619] border border-[#2A2D35] rounded-xl p-5 space-y-4">
      {/* ── Header ── */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-2">
          <Activity size={18} className="text-[#00C087]" />
          <h2 className="text-white font-bold text-lg">Dealer GEX 仪表盘</h2>
          <span className="text-[#8E9299] text-xs">Gamma Exposure</span>
        </div>

        <div className="flex items-center gap-2 flex-wrap">
          {/* Ticker selector */}
          <div className="flex gap-1">
            {TICKERS.map((t) => (
              <button
                key={t}
                onClick={() => setTicker(t)}
                className={cn(
                  "px-2 py-1 rounded text-xs font-mono font-bold border transition-colors",
                  ticker === t
                    ? "bg-[#00C087] text-black border-[#00C087]"
                    : "bg-[#0E1014] text-[#8E9299] border-[#2A2D35] hover:border-[#00C087]/50",
                )}
              >
                {t}
              </button>
            ))}
          </div>

          {/* DTE selector */}
          <select
            value={maxDte}
            onChange={(e) => setMaxDte(Number(e.target.value))}
            className="bg-[#0E1014] border border-[#2A2D35] text-[#8E9299] text-xs rounded px-2 py-1"
          >
            {DTE_OPTIONS.map((d) => (
              <option key={d} value={d}>
                DTE ≤ {d}
              </option>
            ))}
          </select>

          <button
            onClick={() => void refresh()}
            disabled={loading}
            className={cn(
              "flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs font-bold border transition-colors",
              loading
                ? "border-[#2A2D35] text-[#8E9299] cursor-wait"
                : "bg-[#00C087] text-black border-[#00C087] hover:bg-[#00A876]",
            )}
          >
            <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
            {loading ? "加载中…" : "刷新"}
          </button>
        </div>
      </div>

      {/* ── Error ── */}
      {error && (
        <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-3 text-red-400 text-sm">
          ⚠ {error}
        </div>
      )}

      {/* ── Key Levels ── */}
      {snapshot && (
        <>
          <div className="flex flex-wrap gap-3">
            <LevelBadge
              label="Gamma Flip"
              value={snapshot.gamma_flip_level != null ? `$${fmtStrike(snapshot.gamma_flip_level)}` : "—"}
              color="text-yellow-400"
            />
            <LevelBadge
              label="Major Magnet"
              value={snapshot.major_magnet != null ? `$${fmtStrike(snapshot.major_magnet)}` : "—"}
              color="text-[#00C087]"
            />
            <LevelBadge
              label="High Vol Trigger"
              value={snapshot.high_vol_trigger != null ? `$${fmtStrike(snapshot.high_vol_trigger)}` : "—"}
              color="text-red-400"
            />
            <LevelBadge
              label="Net GEX"
              value={fmtDollar(snapshot.net_gex_total)}
              color={isPositive ? "text-[#00C087]" : "text-red-400"}
            />
            <LevelBadge label="当前价" value={`$${snapshot.spot.toFixed(2)}`} color="text-white" />
          </div>

          {/* ── GEX Bar Chart ── */}
          {chartData.length > 0 && (
            <div>
              <div className="text-[#8E9299] text-[11px] font-bold uppercase tracking-wider mb-2">
                各行权价 GEX 分布
              </div>
              <ResponsiveContainer width="100%" height={220}>
                <BarChart data={chartData} margin={{ top: 4, right: 8, left: 4, bottom: 4 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#2A2D35" vertical={false} />
                  <XAxis
                    dataKey="strike"
                    tickFormatter={(v: number) => fmtStrike(v)}
                    tick={{ fill: "#8E9299", fontSize: 10 }}
                    axisLine={{ stroke: "#2A2D35" }}
                    tickLine={false}
                  />
                  <YAxis
                    tickFormatter={(v: number) => fmtDollar(v)}
                    tick={{ fill: "#8E9299", fontSize: 10 }}
                    axisLine={false}
                    tickLine={false}
                    width={60}
                  />
                  <Tooltip content={<GEXTooltip />} />
                  {/* Current spot */}
                  <ReferenceLine
                    x={snapshot.spot}
                    stroke="#FFFFFF"
                    strokeDasharray="4 3"
                    strokeWidth={1.5}
                    label={{ value: "spot", position: "top", fill: "#FFFFFF", fontSize: 9 }}
                  />
                  {/* Gamma Flip */}
                  {snapshot.gamma_flip_level != null && (
                    <ReferenceLine
                      x={snapshot.gamma_flip_level}
                      stroke="#FACC15"
                      strokeDasharray="3 2"
                      strokeWidth={1}
                      label={{ value: "flip", position: "insideTopRight", fill: "#FACC15", fontSize: 9 }}
                    />
                  )}
                  <Bar dataKey="net_gex" isAnimationActive={false} radius={[2, 2, 0, 0]}>
                    {chartData.map((entry, index) => (
                      <Cell
                        key={`cell-${index}`}
                        fill={entry.net_gex >= 0 ? "#00C087" : "#EF4444"}
                        fillOpacity={0.85}
                      />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}

          {/* ── Interpretation ── */}
          <div className="bg-[#0E1014] border border-[#2A2D35] rounded-lg p-3 space-y-1.5 text-xs">
            <div className="text-[#8E9299] text-[10px] font-bold uppercase tracking-wider">
              交易解读
            </div>
            <div className="flex items-start gap-2">
              <TrendingUp size={12} className="text-[#00C087] mt-0.5 shrink-0" />
              <span className="text-[#8E9299]">
                <span className="text-[#00C087]">正 GEX（绿色）</span>
                {" "}→ 做市商 long gamma → 对冲卖高买低 → 价格被钉（mean-reversion）
              </span>
            </div>
            <div className="flex items-start gap-2">
              <TrendingDown size={12} className="text-red-400 mt-0.5 shrink-0" />
              <span className="text-[#8E9299]">
                <span className="text-red-400">负 GEX（红色）</span>
                {" "}→ 做市商 short gamma → 顺势追击 → 波动放大（trending）
              </span>
            </div>
            <div className="flex items-start gap-2">
              <Zap size={12} className="text-yellow-400 mt-0.5 shrink-0" />
              <span className="text-[#8E9299]">
                <span className="text-yellow-400">Gamma Flip</span>
                {" "}= 多空分水岭；价格跌破 High Vol Trigger 则波动加速
              </span>
            </div>
          </div>
        </>
      )}

      {/* ── Empty state ── */}
      {!snapshot && !loading && !error && (
        <div className="text-center py-10 text-[#8E9299] text-sm">
          点击「刷新」加载 {ticker} 期权 GEX 数据
        </div>
      )}
    </section>
  );
}

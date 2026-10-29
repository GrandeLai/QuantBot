/**
 * 账户组合概览面板.
 */
import { useCallback, useEffect, useState } from "react";
import {
  TrendingUp,
  Activity,
  PieChart,
  LayoutGrid,
  Info,
  RefreshCw,
  Wallet,
  BarChart2,
  ShieldCheck,
} from "lucide-react";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import MetricCard from "./ui/MetricCard";
import { cn } from "../lib/utils";

// ── 数据类型（与后端 API 对应）────────────────────────────────────────────────

interface StrategyInfo {
  name: string;
  symbol: string;
  allocation: number;
  portfolio_value: number;
  pnl: number;
  pnl_pct: number;
  bars_processed: number;
  trades_count: number;
  max_drawdown: number;
  sharpe_ratio: number;
}

interface PortfolioSummary {
  total_cash: number;
  total_allocated: number;
  total_portfolio_value: number;
  strategies: StrategyInfo[];
  provider?: string;
  mode?: string;
  total_pnl?: number;
  total_pnl_pct?: number;
  daily_pnl: number;
  daily_pnl_pct: number;
}

interface EquityPoint {
  timestamp: string;
  value: number;
  date: string;
}

// ── 状态小点 ─────────────────────────────────────────────────────────────────

function StatusDot({ status }: { status: "active" | "paused" | "error" }) {
  return (
    <div className="flex items-center gap-2">
      <div
        className={cn(
          "w-1.5 h-1.5 rounded-full",
          status === "active" ? "bg-[#00C087] animate-pulse" :
          status === "paused" ? "bg-[#FFB800]" : "bg-[#FF4D4D]",
        )}
      />
      <span className="text-xs text-[#8E9299] capitalize">{status}</span>
    </div>
  );
}

// ── 主组件 ────────────────────────────────────────────────────────────────────

const PERIOD_OPTIONS = ["1D", "1W", "1M", "ALL"] as const;

export default function PortfolioPanel() {
  const [summary, setSummary] = useState<PortfolioSummary | null>(null);
  const [equityData, setEquityData] = useState<EquityPoint[]>([]);
  const [loading, setLoading] = useState(false);
  const [activePeriod, setActivePeriod] = useState<(typeof PERIOD_OPTIONS)[number]>("1M");

  const loadEquity = useCallback(async (period: (typeof PERIOD_OPTIONS)[number]) => {
    try {
      const res = await fetch(`/api/portfolio/equity?period=${period}`);
      if (res.ok) {
        const d = (await res.json()) as { points: EquityPoint[] };
        setEquityData(d.points);
      }
    } catch (_) { /* 静默 */ }
  }, []);

  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      const sumRes = await fetch("/api/portfolio/summary");
      if (sumRes.ok) setSummary((await sumRes.json()) as PortfolioSummary);
    } catch (_) {
      // 静默处理网络错误
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadData();
    void loadEquity(activePeriod);
  }, [loadData, loadEquity]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── 派生数据
  const strategies = summary?.strategies ?? [];

  const totalPnl = summary?.total_pnl ?? 0;
  const totalPnlPct = summary?.total_pnl_pct ?? 0;

  const dailyPnl = summary?.daily_pnl ?? 0;
  const dailyPnlPct = summary?.daily_pnl_pct ?? 0;

  return (
    <div className="text-[#E1E4E8] font-sans selection:bg-white/10">
      <div className="space-y-6">

        {/* ── 页头 ──────────────────────────────────────── */}
        <div className="flex flex-col md:flex-row md:items-end justify-between gap-4">
          <div>
            <div className="flex items-center gap-2 text-[#8E9299] mb-1">
              <PieChart size={16} />
              <span className="text-xs font-mono uppercase tracking-[0.2em]">
                Portfolio Management
              </span>
            </div>
            <h1 className="text-3xl font-bold text-white tracking-tight">组合概览</h1>
          </div>

          <div className="flex items-center gap-3">
            <button
              onClick={() => void loadData()}
              disabled={loading}
              className="bg-[#151619] border border-[#2A2D35] rounded-lg px-3 py-2 text-[#8E9299] hover:text-white transition-colors disabled:opacity-50 flex items-center gap-2 text-sm"
              title="刷新数据"
            >
              <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
              刷新
            </button>
          </div>
        </div>

        {/* ── 指标卡片 ──────────────────────────── */}
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4">
          <MetricCard
            title="组合总价值"
            value={summary ? `$${summary.total_portfolio_value.toLocaleString()}` : "—"}
            subValue="账户净值"
            icon={Activity}
          />
          <MetricCard
            title="累计盈亏"
            value={
              summary
                ? `${totalPnl >= 0 ? "+" : ""}$${Math.abs(totalPnl).toLocaleString(undefined, { maximumFractionDigits: 0 })}`
                : "—"
            }
            trend={summary ? totalPnlPct : undefined}
            subValue="总回报率"
            icon={TrendingUp}
          />
          <MetricCard
            title="当日盈亏"
            value={`${dailyPnl >= 0 ? "+" : ""}$${Math.abs(dailyPnl).toLocaleString()}`}
            trend={dailyPnlPct}
            subValue="今日变动"
            icon={BarChart2}
          />
          <MetricCard
            title="未分配资金"
            value={summary ? `$${summary.total_cash.toLocaleString()}` : "—"}
            subValue="可用现金"
            icon={Wallet}
          />
          <MetricCard
            title="Broker"
            value={summary?.provider?.toUpperCase() ?? "—"}
            subValue="交易通道"
            icon={LayoutGrid}
          />
          <MetricCard
            title="账户模式"
            value={summary?.mode?.toUpperCase() ?? "—"}
            subValue="执行环境"
            icon={ShieldCheck}
          />
        </div>

        {/* ── 主内容 3列布局 ────────────────────────────── */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">

          {/* 左 2/3：权益曲线 + 上线前验证 */}
          <div className="lg:col-span-2 space-y-6">

            {/* 权益曲线 */}
            <div className="bg-[#151619] border border-[#2A2D35] rounded-xl p-6">
              <div className="flex items-center justify-between mb-6">
                <h3 className="text-white font-semibold flex items-center gap-2">
                  <TrendingUp size={18} className="text-[#00C087]" />
                  净值曲线
                </h3>
                <div className="flex bg-[#1C1E22] rounded-lg p-1">
                  {PERIOD_OPTIONS.map((t) => (
                    <button
                      key={t}
                      onClick={() => { setActivePeriod(t); void loadEquity(t); }}
                      className={cn(
                        "px-3 py-1 text-[10px] font-bold rounded-md transition-all",
                        activePeriod === t
                          ? "bg-[#2A2D35] text-white"
                          : "text-[#8E9299] hover:text-white",
                      )}
                    >
                      {t}
                    </button>
                  ))}
                </div>
              </div>
              <div className="h-[280px] w-full">
                {equityData.length === 0 ? (
                  <div className="h-full flex items-center justify-center text-[#4A4D55] text-sm">
                    暂无历史数据 — 刷新组合后自动记录
                  </div>
                ) : (
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={equityData}>
                    <defs>
                      <linearGradient id="colorPortfolio" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#00C087" stopOpacity={0.3} />
                        <stop offset="95%" stopColor="#00C087" stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1C1E22" vertical={false} />
                    <XAxis
                      dataKey="date"
                      stroke="#4A4D55"
                      fontSize={10}
                      tickLine={false}
                      axisLine={false}
                      tickFormatter={(v: string) => String(v).split("-").slice(1).join("/")}
                    />
                    <YAxis
                      stroke="#4A4D55"
                      fontSize={10}
                      tickLine={false}
                      axisLine={false}
                      tickFormatter={(v: number) => `$${(v / 1_000_000).toFixed(1)}M`}
                      domain={["dataMin - 50000", "dataMax + 50000"]}
                    />
                    <Tooltip
                      contentStyle={{
                        backgroundColor: "#1C1E22",
                        border: "1px solid #2A2D35",
                        borderRadius: 8,
                        fontSize: 12,
                      }}
                      itemStyle={{ color: "#00C087" }}
                      formatter={(v) =>
                        v != null
                          ? [`$${Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`, "净值"]
                          : ["—", "净值"]
                      }
                    />
                    <Area
                      type="monotone"
                      dataKey="value"
                      stroke="#00C087"
                      strokeWidth={2}
                      fillOpacity={1}
                      fill="url(#colorPortfolio)"
                    />
                  </AreaChart>
                </ResponsiveContainer>
                )}
              </div>
            </div>

            {/* 上线前验证 */}
            <div className="bg-[#151619] border border-[#2A2D35] rounded-xl overflow-hidden">
              <div className="px-6 py-4 border-b border-[#2A2D35] flex items-center justify-between">
                <h3 className="text-white font-semibold">
                  上线前验证
                  <span className="ml-2 text-xs text-[#8E9299] font-mono">
                    ({strategies.length})
                  </span>
                </h3>
                <span className="text-[10px] text-[#4A4D55] font-mono">
                  持仓市值 ${summary?.total_allocated.toLocaleString() ?? "—"}
                </span>
              </div>

              {strategies.length === 0 ? (
                <div className="px-6 py-12 text-center text-[#8E9299] text-sm">
                  本地模拟策略槽位已移除；策略上线前请在实盘前验证中完成回测和风险检查。
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-left border-collapse">
                    <thead>
                      <tr className="bg-[#1C1E22]/50 border-y border-[#2A2D35]">
                        {["策略名称", "状态", "分配资金", "盈亏", "回撤", "夏普", ""].map((h) => (
                          <th
                            key={h}
                            className="px-5 py-3 text-[10px] font-mono uppercase tracking-wider text-[#8E9299] whitespace-nowrap"
                          >
                            {h}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-[#2A2D35]">
                      {strategies.map((s) => (
                        <tr
                          key={s.name}
                          className="hover:bg-[#1C1E22]/30 transition-colors group cursor-default"
                        >
                          {/* 策略名 + 标的 */}
                          <td className="px-5 py-4">
                            <div className="flex flex-col">
                              <span className="text-sm font-medium text-white group-hover:text-[#00C087] transition-colors">
                                {s.name}
                              </span>
                              <span className="text-[10px] text-[#8E9299] font-mono">
                                {s.symbol} · {s.bars_processed}K · {s.trades_count}T
                              </span>
                            </div>
                          </td>

                          {/* 状态 */}
                          <td className="px-5 py-4">
                            <StatusDot status="active" />
                          </td>

                          {/* 分配资金 */}
                          <td className="px-5 py-4 text-sm font-mono text-[#E1E4E8]">
                            ${(s.allocation / 1000).toLocaleString()}K
                          </td>

                          {/* 盈亏 */}
                          <td className="px-5 py-4">
                            <div className="flex flex-col">
                              <span
                                className={cn(
                                  "text-sm font-mono",
                                  s.pnl >= 0 ? "text-[#00C087]" : "text-[#FF4D4D]",
                                )}
                              >
                                {s.pnl >= 0 ? "+" : ""}
                                {s.pnl.toFixed(2)}
                              </span>
                              <span
                                className={cn(
                                  "text-[10px] font-mono",
                                  s.pnl_pct >= 0
                                    ? "text-[#00C087]/70"
                                    : "text-[#FF4D4D]/70",
                                )}
                              >
                                {s.pnl_pct >= 0 ? "+" : ""}
                                {s.pnl_pct.toFixed(2)}%
                              </span>
                            </div>
                          </td>

                          {/* 最大回撤 */}
                          <td className="px-5 py-4 text-sm font-mono text-[#FF4D4D]">
                            {s.max_drawdown !== 0
                              ? `${s.max_drawdown.toFixed(2)}%`
                              : <span className="text-[#4A4D55]">—</span>}
                          </td>

                          {/* 夏普比率 */}
                          <td className="px-5 py-4 text-sm font-mono text-[#8E9299]">
                            {s.sharpe_ratio !== 0
                              ? <span className={s.sharpe_ratio >= 1 ? "text-[#00C087]" : s.sharpe_ratio >= 0 ? "text-[#FFB800]" : "text-[#FF4D4D]"}>{s.sharpe_ratio.toFixed(2)}</span>
                              : <span className="text-[#4A4D55]">—</span>}
                          </td>

                          {/* 移除 */}
                          <td className="px-5 py-4 text-right">
                            <button
                              disabled
                              className="text-[#8E9299] hover:text-[#FF4D4D] transition-colors text-xs px-2 py-1 rounded hover:bg-[#FF4D4D]/10"
                            >
                              移除
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>

          {/* 右 1/3：风险洞察 */}
          <div className="space-y-6">
            {/* 风险洞察 */}
            <div className="bg-[#151619] border border-[#2A2D35] rounded-xl p-6">
              <h3 className="text-white font-semibold mb-4 flex items-center gap-2">
                <Info size={18} className="text-[#FFB800]" />
                风险洞察
              </h3>
              <div className="space-y-3">
                {strategies.length === 0 ? (
                  <p className="text-[#8E9299] text-xs">
                    当前组合页只展示 broker 账户快照。策略进入交易执行前，请在实盘前验证中完成回测、回撤和成本敏感性检查。
                  </p>
                ) : (
                  <>
                    {totalPnl < 0 && (
                      <div className="p-3 bg-[#1C1E22] rounded-lg border-l-2 border-[#FF4D4D]">
                        <p className="text-xs text-[#E1E4E8] leading-relaxed">
                          <span className="font-bold text-[#FF4D4D]">警报</span>：组合整体盈亏为负 (
                          {totalPnlPct.toFixed(2)}%)，请关注亏损策略并及时调整仓位。
                        </p>
                      </div>
                    )}
                    <div className="p-3 bg-[#1C1E22] rounded-lg border-l-2 border-[#00C087]">
                      <p className="text-xs text-[#E1E4E8] leading-relaxed">
                        <span className="font-bold text-[#00C087]">建议</span>：当前共{" "}
                        {strategies.length} 个策略，总分配{" "}
                        ${summary?.total_allocated.toLocaleString()}。
                        请继续观察回撤、成本和成交质量。
                      </p>
                    </div>
                  </>
                )}
              </div>
            </div>

            {/* 组合分配饼图（占位） */}
            {strategies.length > 0 && (
              <div className="bg-[#151619] border border-[#2A2D35] rounded-xl p-6">
                <h3 className="text-white font-semibold mb-4 text-sm">资金分配</h3>
                <div className="space-y-2">
                  {strategies.map((s) => {
                    const pct = summary
                      ? (s.allocation / summary.total_portfolio_value) * 100
                      : 0;
                    return (
                      <div key={s.name} className="flex flex-col gap-1">
                        <div className="flex justify-between text-[10px]">
                          <span className="text-[#8E9299] truncate max-w-[70%]">{s.name}</span>
                          <span className="text-white font-mono">{pct.toFixed(1)}%</span>
                        </div>
                        <div className="h-1 bg-[#1C1E22] rounded-full overflow-hidden">
                          <div
                            className="h-full bg-[#00C087] rounded-full transition-all"
                            style={{ width: `${Math.min(pct, 100)}%` }}
                          />
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </div>

        </div>
      </div>
    </div>
  );
}

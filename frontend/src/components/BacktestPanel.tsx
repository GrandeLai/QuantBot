/**
 * 回测面板 — 实盘前验证测试.
 * 流程: 选策略 → 配置参数 → 拉 K 线 → POST /backtest/run → 展示验证绩效
 */
import { useEffect, useRef, useState } from "react";
import {
  History,
  Play,
  BarChart3,
  TrendingUp,
  TrendingDown,
  ShieldAlert,
  Percent,
  RefreshCw,
  ChevronDown,
  ChevronUp,
} from "lucide-react";
import FeatureGuideButton from "@/components/guides/FeatureGuideButton";
import { cn } from "../lib/utils";

// ── 数据类型 ──────────────────────────────────────────────────────────────────

interface AvailableStrategy {
  id: string;
  name: string;
  description: string;
  default_params: Record<string, unknown>;
  source?: "template" | "user";
  version?: string;
}

interface BacktestMetrics {
  total_return: number;
  annual_return: number;
  max_drawdown: number;
  sharpe_ratio: number;
  sortino_ratio: number;
  calmar_ratio: number;
  volatility: number;
  win_rate: number;
  profit_factor: number;
  total_trades: number;
  win_trades: number;
  loss_trades: number;
  avg_win: number;
  avg_loss: number;
  initial_cash: number;
  final_value: number;
  start_date: string;
  end_date: string;
  trading_days: number;
}

export interface BacktestResult {
  symbol: string;
  timeframe: string;
  strategy_id: string;
  bars_processed: number;
  duration_seconds: number;
  trades_count: number;
  metrics: BacktestMetrics;
}

// ── 样式 token ────────────────────────────────────────────────────────────────

const INPUT_CLS =
  "w-full h-9 bg-[#161b22] border border-[#30363d] rounded-lg px-3 text-sm text-white outline-none focus:border-blue-500/50 transition-colors placeholder-[#8b949e]/50";

const TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d", "1w"];

// ── 辅助组件 ─────────────────────────────────────────────────────────────────

function MetricTile({
  label,
  value,
  sub,
  color = "text-white",
  icon: Icon,
}: {
  label: string;
  value: string;
  sub?: string;
  color?: string;
  icon?: React.ElementType;
}) {
  return (
    <div className="bg-[#161b22] border border-[#30363d] rounded-xl p-4">
      <div className="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-wider text-[#8b949e] mb-3">
        {Icon && <Icon className="w-3 h-3" />}
        {label}
      </div>
      <div className={cn("text-2xl font-bold tracking-tight", color)}>{value}</div>
      {sub && <div className="text-[10px] text-[#8b949e] mt-1">{sub}</div>}
    </div>
  );
}

// ── 主组件 ────────────────────────────────────────────────────────────────────

interface BacktestPanelProps {
  onResult?: (result: BacktestResult) => void;
}

export default function BacktestPanel({ onResult }: BacktestPanelProps = {}) {
  // ── 配置状态
  const [strategies, setStrategies] = useState<AvailableStrategy[]>([]);
  const [strategyId, setStrategyId] = useState("");
  const [symbol, setSymbol] = useState("AAPL");
  const [timeframe, setTimeframe] = useState("1d");
  const [limit, setLimit] = useState(300);
  const [initialCash, setInitialCash] = useState(1_000_000);
  const [commissionRate, setCommissionRate] = useState(0.001);
  const [slippagePct, setSlippagePct] = useState(0.0005);
  const [stopLossPct, setStopLossPct] = useState<number | "">("");
  const [takeProfitPct, setTakeProfitPct] = useState<number | "">("");
  const [showAdvanced, setShowAdvanced] = useState(false);

  // ── 运行状态
  const [running, setRunning] = useState(false);
  const [status, setStatus] = useState<"idle" | "fetchingBars" | "running" | "done" | "error">("idle");
  const [errorMsg, setErrorMsg] = useState("");
  const [result, setResult] = useState<BacktestResult | null>(null);

  const abortRef = useRef<AbortController | null>(null);

  // ── 加载可用策略列表
  useEffect(() => {
    void (async () => {
      try {
        const res = await fetch("/api/portfolio/available-strategies");
        if (res.ok) {
          const data = (await res.json()) as { strategies: AvailableStrategy[] };
          setStrategies(data.strategies);
          if (data.strategies.length > 0) setStrategyId(data.strategies[0].id);
        }
      } catch { /* 后端未启动时静默 */ }
    })();
  }, []);

  const selectedStrategy = strategies.find((s) => s.id === strategyId);

  // ── 执行回测
  const runBacktest = async () => {
    if (!strategyId) return;
    setRunning(true);
    setStatus("fetchingBars");
    setErrorMsg("");
    setResult(null);
    abortRef.current = new AbortController();

    try {
      // Step 1: 获取 K 线数据
      const barsRes = await fetch(
        `/api/data/bars?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}&limit=${limit}`,
        { signal: abortRef.current.signal },
      );
      if (!barsRes.ok) {
        const text = await barsRes.text();
        throw new Error(`获取 K 线失败: ${text}`);
      }
      const barsJson = (await barsRes.json()) as { bars: unknown[] };
      if (!barsJson.bars || barsJson.bars.length < 2) {
        throw new Error("K 线数据不足（需 ≥ 2 根），请先在「看盘」Tab 拉取历史数据");
      }

      setStatus("running");

      // Step 2: 提交回测请求
      const req = {
        symbol,
        timeframe,
        bars: barsJson.bars,
        strategy_id: strategyId,
        strategy_params: selectedStrategy?.default_params ?? {},
        initial_cash: initialCash,
        commission_rate: commissionRate,
        slippage_pct: slippagePct,
        stop_loss_pct: stopLossPct === "" ? null : stopLossPct,
        take_profit_pct: takeProfitPct === "" ? null : takeProfitPct,
      };

      const runRes = await fetch("/api/backtest/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(req),
        signal: abortRef.current.signal,
      });

      if (!runRes.ok) {
        const text = await runRes.text();
        throw new Error(`回测失败: ${text}`);
      }

      const data = (await runRes.json()) as BacktestResult;
      setResult(data);
      setStatus("done");
      onResult?.(data);
    } catch (e: unknown) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      setErrorMsg(e instanceof Error ? e.message : String(e));
      setStatus("error");
    } finally {
      setRunning(false);
    }
  };

  // ── 格式化函数
  const pct = (v: number) => `${(v * 100).toFixed(2)}%`;
  const money = (v: number) =>
    v >= 1_000_000
      ? `$${(v / 1_000_000).toFixed(2)}M`
      : `$${v.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;

  return (
    <div
      className="relative flex bg-[#0d1117] rounded-xl overflow-hidden border border-[#30363d]"
      style={{ height: "calc(100vh - 120px)" }}
    >
      {/* ── 左侧配置栏 ────────────────────────────────── */}
      <aside className="w-72 shrink-0 border-r border-[#30363d] bg-[#0d1117] flex flex-col">
        {/* 标题 */}
        <div className="px-6 pt-8 pb-6 border-b border-[#30363d]">
          <div className="flex items-center gap-2 text-[10px] font-bold text-[#8b949e] uppercase tracking-[0.2em] mb-2">
            <History className="w-3 h-3" />
            Backtesting
          </div>
          <h1 className="text-2xl font-bold text-white">回测引擎</h1>
        </div>

        {/* 配置表单 */}
        <div className="flex-1 overflow-y-auto p-6 space-y-5">

          {/* 策略选择 */}
          <div className="space-y-2">
            <label className="text-[10px] uppercase font-bold text-[#8b949e] tracking-wider block">策略</label>
            <select
              value={strategyId}
              onChange={(e) => setStrategyId(e.target.value)}
              className="w-full h-9 bg-[#161b22] border border-[#30363d] rounded-lg px-3 text-sm text-white outline-none focus:border-blue-500/50 transition-colors appearance-none cursor-pointer"
            >
              {strategies.length === 0 ? (
                <option value="">后端未连接</option>
              ) : (
                strategies.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.source === "user" ? `[用户] ${s.name}` : `[模板] ${s.name}`}
                  </option>
                ))
              )}
            </select>
            {selectedStrategy && (
              <p className="text-[10px] text-[#8b949e] leading-relaxed">
                {selectedStrategy.description}
                <span className="ml-2 inline-flex items-center rounded-full border border-[#30363d] bg-[#161b22] px-2 py-0.5 text-[9px] uppercase tracking-wider text-[#8b949e]">
                  {selectedStrategy.source === "user" ? "User Strategy" : "Template"}
                </span>
              </p>
            )}
          </div>

          {/* 标的 */}
          <div className="space-y-2">
            <label className="text-[10px] uppercase font-bold text-[#8b949e] tracking-wider block">标的代码</label>
            <input
              value={symbol}
              onChange={(e) => setSymbol(e.target.value.toUpperCase())}
              className={INPUT_CLS}
              placeholder="AAPL"
            />
          </div>

          {/* 时间周期 */}
          <div className="space-y-2">
            <label className="text-[10px] uppercase font-bold text-[#8b949e] tracking-wider block">K 线周期</label>
            <div className="grid grid-cols-4 gap-1">
              {TIMEFRAMES.map((tf) => (
                <button
                  key={tf}
                  onClick={() => setTimeframe(tf)}
                  className={cn(
                    "h-8 rounded-md text-xs font-mono font-bold transition-all",
                    timeframe === tf
                      ? "bg-blue-600 text-white"
                      : "bg-[#161b22] text-[#8b949e] hover:text-white border border-[#30363d]",
                  )}
                >
                  {tf}
                </button>
              ))}
            </div>
          </div>

          {/* K 线数量 */}
          <div className="space-y-2">
            <label className="text-[10px] uppercase font-bold text-[#8b949e] tracking-wider block">
              K 线数量 <span className="text-blue-400 font-mono normal-case">{limit}</span>
            </label>
            <input
              type="range"
              min={50}
              max={1000}
              step={50}
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value))}
              className="w-full accent-blue-500"
            />
            <div className="flex justify-between text-[9px] text-[#434651]">
              <span>50</span><span>500</span><span>1000</span>
            </div>
          </div>

          {/* 初始资金 */}
          <div className="space-y-2">
            <label className="text-[10px] uppercase font-bold text-[#8b949e] tracking-wider block">初始资金 ($)</label>
            <input
              type="number"
              value={initialCash}
              onChange={(e) => setInitialCash(Number(e.target.value))}
              className={INPUT_CLS}
              step={100000}
              min={10000}
            />
          </div>

          {/* 高级参数（可折叠）*/}
          <div>
            <button
              onClick={() => setShowAdvanced((v) => !v)}
              className="flex items-center gap-2 text-[10px] uppercase font-bold text-[#8b949e] tracking-wider hover:text-white transition-colors w-full"
            >
              高级参数
              {showAdvanced ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
            </button>

            {showAdvanced && (
              <div className="mt-3 space-y-3 pl-2 border-l border-[#30363d]">
                <div className="space-y-1">
                  <label className="text-[9px] uppercase text-[#8b949e] tracking-wider block">
                    手续费率 ({(commissionRate * 100).toFixed(2)}%)
                  </label>
                  <input
                    type="number"
                    value={commissionRate}
                    onChange={(e) => setCommissionRate(Number(e.target.value))}
                    className={INPUT_CLS}
                    step={0.0001}
                    min={0}
                    max={0.01}
                  />
                </div>
                <div className="space-y-1">
                  <label className="text-[9px] uppercase text-[#8b949e] tracking-wider block">
                    滑点率 ({(slippagePct * 100).toFixed(3)}%)
                  </label>
                  <input
                    type="number"
                    value={slippagePct}
                    onChange={(e) => setSlippagePct(Number(e.target.value))}
                    className={INPUT_CLS}
                    step={0.0001}
                    min={0}
                    max={0.01}
                  />
                </div>
                <div className="space-y-1">
                  <label className="text-[9px] uppercase text-[#8b949e] tracking-wider block">止损比例 (留空禁用)</label>
                  <input
                    type="number"
                    value={stopLossPct}
                    onChange={(e) => setStopLossPct(e.target.value === "" ? "" : Number(e.target.value))}
                    className={INPUT_CLS}
                    placeholder="如: 0.05"
                    step={0.01}
                    min={0}
                    max={1}
                  />
                </div>
                <div className="space-y-1">
                  <label className="text-[9px] uppercase text-[#8b949e] tracking-wider block">止盈比例 (留空禁用)</label>
                  <input
                    type="number"
                    value={takeProfitPct}
                    onChange={(e) => setTakeProfitPct(e.target.value === "" ? "" : Number(e.target.value))}
                    className={INPUT_CLS}
                    placeholder="如: 0.10"
                    step={0.01}
                    min={0}
                    max={10}
                  />
                </div>
              </div>
            )}
          </div>
        </div>

        {/* 运行按钮 */}
        <div className="p-6 border-t border-[#30363d] space-y-3">
          <button
            onClick={() => void runBacktest()}
            disabled={running || !strategyId}
            className="w-full h-10 bg-white text-black font-bold rounded-lg hover:bg-[#e1e4e8] transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2 text-sm"
          >
            {running ? (
              <>
                <RefreshCw className="w-4 h-4 animate-spin" />
                {status === "fetchingBars" ? "拉取 K 线…" : "运行中…"}
              </>
            ) : (
              <>
                <Play className="w-4 h-4 fill-current" />
                运行回测
              </>
            )}
          </button>
          {running && (
            <button
              onClick={() => abortRef.current?.abort()}
              className="w-full h-8 text-xs text-[#8b949e] hover:text-white border border-[#30363d] rounded-lg transition-colors"
            >
              取消
            </button>
          )}
        </div>
      </aside>

      {/* ── 右侧结果区 ────────────────────────────────── */}
      <main className="flex-1 overflow-y-auto p-8">

        {status === "idle" && (
          <div className="h-full flex flex-col items-center justify-center text-center gap-4">
            <div className="w-16 h-16 rounded-2xl bg-[#161b22] border border-[#30363d] flex items-center justify-center">
              <BarChart3 className="w-8 h-8 text-[#8b949e]" />
            </div>
            <div>
              <p className="text-white font-semibold text-lg">配置并运行回测</p>
              <p className="text-[#8b949e] text-sm mt-1">
                在左侧选择策略、标的和参数，然后点击「运行回测」
              </p>
            </div>
            <p className="text-[10px] text-[#434651] font-mono">
              注：需先在「看盘 → 走势图」拉取标的历史数据
            </p>
          </div>
        )}

        {(status === "fetchingBars" || status === "running") && (
          <div className="h-full flex flex-col items-center justify-center gap-6">
            <RefreshCw className="w-10 h-10 text-blue-400 animate-spin" />
            <div className="text-center">
              <p className="text-white font-semibold">
                {status === "fetchingBars" ? "正在获取历史 K 线数据…" : "回测运行中…"}
              </p>
              <p className="text-[#8b949e] text-sm mt-1">
                {symbol} · {timeframe} · {limit} 根 K 线
              </p>
            </div>
          </div>
        )}

        {status === "error" && (
          <div className="h-full flex flex-col items-center justify-center gap-4">
            <div className="w-14 h-14 rounded-full bg-red-500/10 flex items-center justify-center">
              <ShieldAlert className="w-7 h-7 text-red-400" />
            </div>
            <div className="text-center max-w-md">
              <p className="text-red-400 font-semibold mb-2">回测失败</p>
              <p className="text-[#8b949e] text-sm leading-relaxed">{errorMsg}</p>
            </div>
            <button
              onClick={() => setStatus("idle")}
              className="text-xs text-[#8b949e] hover:text-white underline"
            >
              返回配置
            </button>
          </div>
        )}

        {status === "done" && result && (
          <div className="space-y-8">
            {/* 摘要标题 */}
            <div className="flex items-end justify-between">
              <div>
                <div className="flex items-center gap-2 text-[10px] font-bold text-[#8b949e] uppercase tracking-[0.2em] mb-1">
                  <History className="w-3 h-3" />
                  回测结果
                </div>
                <h2 className="text-3xl font-bold text-white">
                  {strategies.find((s) => s.id === result.strategy_id)?.name ?? result.strategy_id}
                </h2>
                <p className="text-[#8b949e] text-sm mt-0.5">
                  {result.symbol} · {result.timeframe} · {result.bars_processed} 根 K 线 ·{" "}
                  {result.duration_seconds.toFixed(2)}s
                </p>
              </div>
              <button
                onClick={() => setStatus("idle")}
                className="text-xs text-[#8b949e] hover:text-white border border-[#30363d] rounded-lg px-3 py-2 transition-colors"
              >
                重新配置
              </button>
            </div>

            {/* 核心指标 6 宫格 */}
            <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
              <MetricTile
                label="总收益率"
                value={pct(result.metrics.total_return)}
                sub="策略期间总回报"
                color={result.metrics.total_return >= 0 ? "text-green-400" : "text-red-400"}
                icon={result.metrics.total_return >= 0 ? TrendingUp : TrendingDown}
              />
              <MetricTile
                label="年化收益率"
                value={pct(result.metrics.annual_return)}
                sub="等效年化"
                color={result.metrics.annual_return >= 0 ? "text-green-400" : "text-red-400"}
                icon={Percent}
              />
              <MetricTile
                label="最大回撤"
                value={pct(result.metrics.max_drawdown)}
                sub="最大峰谷跌幅"
                color="text-red-400"
                icon={ShieldAlert}
              />
              <MetricTile
                label="夏普比率"
                value={result.metrics.sharpe_ratio.toFixed(3)}
                sub="风险调整后收益"
                color={result.metrics.sharpe_ratio >= 1 ? "text-green-400" : result.metrics.sharpe_ratio >= 0 ? "text-yellow-400" : "text-red-400"}
                icon={BarChart3}
              />
              <MetricTile
                label="胜率"
                value={pct(result.metrics.win_rate)}
                sub={`${result.metrics.win_trades}W / ${result.metrics.loss_trades}L`}
                color={result.metrics.win_rate >= 0.5 ? "text-green-400" : "text-orange-400"}
              />
              <MetricTile
                label="交易次数"
                value={String(result.metrics.total_trades)}
                sub="期间完整交易"
              />
            </div>

            {/* 详细指标表 */}
            <div className="bg-[#161b22] border border-[#30363d] rounded-xl overflow-hidden">
              <div className="px-6 py-4 border-b border-[#30363d]">
                <h3 className="text-sm font-bold text-white uppercase tracking-wider">详细绩效指标</h3>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 divide-y sm:divide-y-0 sm:divide-x divide-[#30363d]">
                {/* 左列 */}
                <div className="divide-y divide-[#30363d]">
                  {[
                    ["Sortino 比率", result.metrics.sortino_ratio.toFixed(3)],
                    ["Calmar 比率", result.metrics.calmar_ratio.toFixed(3)],
                    ["年化波动率", pct(result.metrics.volatility)],
                    ["盈利因子", result.metrics.profit_factor.toFixed(3)],
                    ["平均盈利", money(result.metrics.avg_win)],
                    ["平均亏损", money(result.metrics.avg_loss)],
                  ].map(([k, v]) => (
                    <div key={k} className="flex justify-between items-center px-6 py-3">
                      <span className="text-xs text-[#8b949e]">{k}</span>
                      <span className="text-xs font-mono text-white">{v}</span>
                    </div>
                  ))}
                </div>
                {/* 右列 */}
                <div className="divide-y divide-[#30363d]">
                  {[
                    ["初始资金", money(result.metrics.initial_cash)],
                    ["最终净值", money(result.metrics.final_value)],
                    ["净利润", money(result.metrics.final_value - result.metrics.initial_cash)],
                    ["交易天数", `${result.metrics.trading_days} 天`],
                    ["起始日期", result.metrics.start_date?.split("T")[0] ?? "—"],
                    ["结束日期", result.metrics.end_date?.split("T")[0] ?? "—"],
                  ].map(([k, v]) => (
                    <div key={k} className="flex justify-between items-center px-6 py-3">
                      <span className="text-xs text-[#8b949e]">{k}</span>
                      <span className="text-xs font-mono text-white">{v}</span>
                    </div>
                  ))}
                </div>
              </div>
            </div>

          </div>
        )}
      </main>
      <FeatureGuideButton guideKey="backtest.workspace" className="bottom-5 right-5" />
    </div>
  );
}

/**
 * Walk-Forward 验证面板 — 滚动窗口样本外测试（接 POST /api/walk-forward）.
 * 流程: 配置参数 → 拉 K 线（stock-assistant）→ POST /api/walk-forward（Rust）→ 展示结果
 */
import { useRef, useState } from "react";
import { GitBranch, Play, RefreshCw, ShieldAlert } from "lucide-react";
import { cn } from "@/lib/utils";
import EquityCurveChart from "@/components/EquityCurveChart";

// ── 数据类型 ──────────────────────────────────────────────────────────────────

interface WindowSplit {
  train_start: number;
  train_end: number;
  test_start: number;
  test_end: number;
}

interface WalkForwardResponse {
  n_windows: number;
  equity_curve: number[];
  window_splits: WindowSplit[];
}

// ── 样式 token ────────────────────────────────────────────────────────────────

const INPUT_CLS =
  "w-full h-9 bg-[#161b22] border border-[#30363d] rounded-lg px-3 text-sm text-white outline-none focus:border-blue-500/50 transition-colors placeholder-[#8b949e]/50";

const TIMEFRAMES = ["1d", "1w", "1h", "4h"];

// ── 主组件 ────────────────────────────────────────────────────────────────────

export default function WalkForwardPanel() {
  // ── 配置状态
  const [symbol, setSymbol] = useState("AAPL");
  const [timeframe, setTimeframe] = useState("1d");
  const [fastPeriod, setFastPeriod] = useState(10);
  const [slowPeriod, setSlowPeriod] = useState(30);
  const [initialCash, setInitialCash] = useState(1_000_000);
  const [trainSize, setTrainSize] = useState(100);
  const [testSize, setTestSize] = useState(50);

  // ── 运行状态
  const [running, setRunning] = useState(false);
  const [status, setStatus] = useState<"idle" | "fetchingBars" | "running" | "done" | "error">("idle");
  const [errorMsg, setErrorMsg] = useState("");
  const [result, setResult] = useState<WalkForwardResponse | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // ── 执行 walk-forward
  const runWalkForward = async () => {
    // 客户端参数校验
    if (fastPeriod >= slowPeriod) {
      setErrorMsg("快线周期必须小于慢线周期");
      setStatus("error");
      return;
    }
    if (trainSize <= slowPeriod) {
      setErrorMsg(`训练窗口 (${trainSize}) 必须大于慢线周期 (${slowPeriod})，否则无法计算 MA`);
      setStatus("error");
      return;
    }

    setRunning(true);
    setStatus("fetchingBars");
    setErrorMsg("");
    setResult(null);
    abortRef.current = new AbortController();

    try {
      // Step 1: 获取 K 线数据（stock-assistant proxy）
      const barsRes = await fetch(
        `/api/data/bars?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}&limit=500`,
        { signal: abortRef.current.signal },
      );
      if (!barsRes.ok) throw new Error(`获取 K 线失败: ${await barsRes.text()}`);

      const barsJson = (await barsRes.json()) as { bars: Array<{ close: number }> };
      if (!barsJson.bars || barsJson.bars.length < 2) {
        throw new Error("K 线数据不足（需 ≥ 2 根），请先在「看盘」拉取历史数据");
      }
      const closes = barsJson.bars.map((b) => b.close);

      setStatus("running");

      // Step 2: 提交 walk-forward 请求（Rust quant-assistant proxy）
      const req = {
        closes,
        fast_period: fastPeriod,
        slow_period: slowPeriod,
        initial_cash: initialCash,
        train_size: trainSize,
        test_size: testSize,
      };

      const wfRes = await fetch("/api/walk-forward", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(req),
        signal: abortRef.current.signal,
      });

      if (!wfRes.ok) throw new Error(`Walk-Forward 验证失败: ${await wfRes.text()}`);

      const data = (await wfRes.json()) as WalkForwardResponse;
      setResult(data);
      setStatus("done");
    } catch (e: unknown) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      setErrorMsg(e instanceof Error ? e.message : String(e));
      setStatus("error");
    } finally {
      setRunning(false);
    }
  };

  return (
    <div
      className="relative flex bg-[#0d1117] rounded-xl overflow-hidden border border-[#30363d]"
      style={{ height: "calc(100vh - 120px)" }}
    >
      {/* ── 左侧配置栏 */}
      <aside className="w-72 shrink-0 border-r border-[#30363d] bg-[#0d1117] flex flex-col">
        <div className="px-6 pt-8 pb-6 border-b border-[#30363d]">
          <div className="flex items-center gap-2 text-[10px] font-bold text-[#8b949e] uppercase tracking-[0.2em] mb-2">
            <GitBranch className="w-3 h-3" />
            Walk-Forward
          </div>
          <h1 className="text-2xl font-bold text-white">样本外验证</h1>
        </div>

        <div className="flex-1 overflow-y-auto p-6 space-y-5">
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

          {/* MA 参数 */}
          <div className="space-y-2">
            <label className="text-[10px] uppercase font-bold text-[#8b949e] tracking-wider block">快线周期 (Fast MA)</label>
            <input
              type="number"
              value={fastPeriod}
              onChange={(e) => setFastPeriod(Math.max(1, Number(e.target.value)))}
              className={INPUT_CLS}
              min={1}
              max={200}
            />
          </div>

          <div className="space-y-2">
            <label className="text-[10px] uppercase font-bold text-[#8b949e] tracking-wider block">慢线周期 (Slow MA)</label>
            <input
              type="number"
              value={slowPeriod}
              onChange={(e) => setSlowPeriod(Math.max(1, Number(e.target.value)))}
              className={INPUT_CLS}
              min={1}
              max={500}
            />
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

          {/* 窗口参数 */}
          <div className="space-y-2">
            <label className="text-[10px] uppercase font-bold text-[#8b949e] tracking-wider block">训练窗口 (Train Size, bars)</label>
            <input
              type="number"
              value={trainSize}
              onChange={(e) => setTrainSize(Math.max(1, Number(e.target.value)))}
              className={INPUT_CLS}
              min={1}
            />
          </div>

          <div className="space-y-2">
            <label className="text-[10px] uppercase font-bold text-[#8b949e] tracking-wider block">测试窗口 (Test Size, bars)</label>
            <input
              type="number"
              value={testSize}
              onChange={(e) => setTestSize(Math.max(1, Number(e.target.value)))}
              className={INPUT_CLS}
              min={1}
            />
          </div>
        </div>

        {/* 运行按钮 */}
        <div className="p-6 border-t border-[#30363d] space-y-3">
          <button
            onClick={() => void runWalkForward()}
            disabled={running}
            className="w-full h-10 bg-white text-black font-bold rounded-lg hover:bg-[#e1e4e8] transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2 text-sm"
          >
            {running ? (
              <>
                <RefreshCw className="w-4 h-4 animate-spin" />
                {status === "fetchingBars" ? "拉取 K 线…" : "验证中…"}
              </>
            ) : (
              <>
                <Play className="w-4 h-4 fill-current" />
                运行验证
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

      {/* ── 右侧结果区 */}
      <main className="flex-1 overflow-y-auto p-8">
        {status === "idle" && (
          <div className="h-full flex flex-col items-center justify-center text-center gap-4">
            <div className="w-16 h-16 rounded-2xl bg-[#161b22] border border-[#30363d] flex items-center justify-center">
              <GitBranch className="w-8 h-8 text-[#8b949e]" />
            </div>
            <div>
              <p className="text-white font-semibold text-lg">Walk-Forward 样本外验证</p>
              <p className="text-[#8b949e] text-sm mt-1">
                设置训练/测试窗口大小，滚动切分验证策略稳健性
              </p>
            </div>
            <p className="text-[10px] text-[#434651] font-mono">注：需先在「看盘」拉取标的历史数据</p>
          </div>
        )}

        {(status === "fetchingBars" || status === "running") && (
          <div className="h-full flex flex-col items-center justify-center gap-6">
            <RefreshCw className="w-10 h-10 text-blue-400 animate-spin" />
            <div className="text-center">
              <p className="text-white font-semibold">
                {status === "fetchingBars" ? "正在获取历史 K 线数据…" : "Walk-Forward 验证中…"}
              </p>
              <p className="text-[#8b949e] text-sm mt-1">
                {symbol} · {timeframe} · 训练 {trainSize} / 测试 {testSize} bars
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
              <p className="text-red-400 font-semibold mb-2">验证失败</p>
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
            {/* 标题行 */}
            <div className="flex items-end justify-between">
              <div>
                <div className="flex items-center gap-2 text-[10px] font-bold text-[#8b949e] uppercase tracking-[0.2em] mb-1">
                  <GitBranch className="w-3 h-3" />
                  Walk-Forward 结果
                </div>
                <h2 className="text-3xl font-bold text-white">
                  MA({fastPeriod},{slowPeriod}) · {symbol}
                </h2>
                <p className="text-[#8b949e] text-sm mt-0.5">
                  共 {result.n_windows} 个窗口 · 训练 {trainSize} bars / 测试 {testSize} bars
                </p>
              </div>
              <button
                onClick={() => setStatus("idle")}
                className="text-xs text-[#8b949e] hover:text-white border border-[#30363d] rounded-lg px-3 py-2 transition-colors"
              >
                重新配置
              </button>
            </div>

            {/* 拼接权益曲线 */}
            <div className="bg-[#161b22] border border-[#30363d] rounded-xl overflow-hidden">
              <div className="px-6 py-3 border-b border-[#30363d]">
                <h3 className="text-[10px] font-bold text-[#8b949e] uppercase tracking-wider">
                  样本外拼接权益曲线
                </h3>
              </div>
              <div className="px-4 py-3">
                <EquityCurveChart
                  data={result.equity_curve}
                  initialCash={initialCash}
                />
              </div>
            </div>

            {/* Window 汇总表格 */}
            <div className="bg-[#161b22] border border-[#30363d] rounded-xl overflow-hidden">
              <div className="px-6 py-4 border-b border-[#30363d] flex items-center justify-between">
                <h3 className="text-sm font-bold text-white uppercase tracking-wider">窗口明细</h3>
                <span className="text-[10px] text-[#8b949e] font-mono">共 {result.n_windows} 个窗口</span>
              </div>
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-[#30363d]">
                    {["窗口", "训练区间", "测试区间"].map((h) => (
                      <th
                        key={h}
                        className="px-6 py-3 text-left text-[10px] font-bold text-[#8b949e] uppercase tracking-wider"
                      >
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {result.window_splits.map((w, i) => (
                    <tr key={i} className="border-b border-[#30363d] last:border-0 hover:bg-[#0d1117]/50">
                      <td className="px-6 py-3 text-[#8b949e] font-mono text-xs">#{i + 1}</td>
                      <td className="px-6 py-3 text-white font-mono text-xs">
                        bar {w.train_start}–{w.train_end}
                      </td>
                      <td className="px-6 py-3 text-blue-400 font-mono text-xs">
                        bar {w.test_start}–{w.test_end}
                      </td>
                    </tr>
                  ))}
                  {/* 汇总行 */}
                  <tr className="bg-[#0d1117]/70">
                    <td className="px-6 py-3 text-[10px] font-bold text-[#8b949e] uppercase tracking-wider" colSpan={3}>
                      合计：{result.n_windows} 个 walk-forward 窗口
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}

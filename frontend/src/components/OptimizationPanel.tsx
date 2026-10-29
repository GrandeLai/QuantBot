/**
 * 策略参数优化面板.
 */
import { useState } from "react";
import { cn } from "../lib/utils";

interface OptResult {
  best: { params: Record<string, number>; sharpe_ratio: number; total_return: number };
  total_combinations: number;
  results: Array<{ params: Record<string, number>; sharpe_ratio: number; total_return: number }>;
}

export default function OptimizationPanel() {
  const [strategyName, setStrategyName] = useState("");
  const [paramJson, setParamJson] = useState(
    '{"fast": [3, 5, 8], "slow": [15, 20, 30]}',
  );
  const [result, setResult] = useState<OptResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const runGridSearch = async () => {
    setLoading(true);
    setError(null);
    try {
      let paramGrid: Record<string, number[]>;
      try {
        paramGrid = JSON.parse(paramJson) as Record<string, number[]>;
      } catch {
        setError("参数格式错误，请输入合法 JSON");
        return;
      }
      const res = await fetch("/api/optimize/grid-search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          strategy_name: strategyName,
          config: { symbol: "TEST", timeframe: "1d", initial_cash: 100000 },
          bars: [],
          param_grid: paramGrid,
        }),
      });
      if (!res.ok) {
        const e = (await res.json()) as { detail: string };
        setError(e.detail);
        return;
      }
      setResult((await res.json()) as OptResult);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex gap-4">
      {/* ── 左侧：配置 ────────────────────────────── */}
      <div className="w-72 shrink-0 bg-[#151619] border border-[#2A2D35] rounded-xl p-5 flex flex-col gap-3">
        <p className="text-xs font-mono uppercase tracking-wider text-[#8E9299]">
          参数优化配置
        </p>

        <div className="flex flex-col gap-1">
          <label className="text-[10px] text-[#8E9299]">策略名称</label>
          <input
            value={strategyName}
            onChange={(e) => setStrategyName(e.target.value)}
            placeholder="如 sma_crossover"
            className="w-full bg-[#1C1E22] border border-[#2A2D35] rounded-lg px-3 py-1.5 text-xs text-white placeholder-[#4A4D55] outline-none focus:border-[#4A4D55] transition-colors"
          />
        </div>

        <div className="flex flex-col gap-1">
          <label className="text-[10px] text-[#8E9299]">参数网格 (JSON)</label>
          <textarea
            value={paramJson}
            onChange={(e) => setParamJson(e.target.value)}
            rows={5}
            className="w-full bg-[#1C1E22] border border-[#2A2D35] rounded-lg px-3 py-2 text-xs text-white outline-none focus:border-[#4A4D55] resize-y transition-colors"
            style={{ fontFamily: "'JetBrains Mono', monospace" }}
          />
        </div>

        <button
          onClick={() => void runGridSearch()}
          disabled={loading}
          className="py-2 bg-blue-600 hover:bg-blue-500 text-white rounded-lg text-xs font-semibold transition-colors disabled:opacity-50"
        >
          {loading ? "优化中…" : "网格搜索"}
        </button>

        {error && <p className="text-xs text-[#FF4D4D]">{error}</p>}
      </div>

      {/* ── 右侧：结果 ────────────────────────────── */}
      <div className="flex-1 bg-[#151619] border border-[#2A2D35] rounded-xl p-6">
        {result ? (
          <div>
            <div className="flex items-center justify-between mb-5">
              <p className="text-sm font-semibold text-white">
                优化结果
                <span className="ml-2 text-xs text-[#8E9299] font-normal">
                  ({result.total_combinations} 组合)
                </span>
              </p>
              <span className="text-xs text-[#8E9299] font-mono">
                最优 Sharpe = {result.best.sharpe_ratio.toFixed(3)}
              </span>
            </div>

            {/* 最优参数 */}
            <div className="bg-[#1C1E22] rounded-xl px-4 py-3 mb-4 border-l-2 border-blue-500">
              <p className="text-[10px] text-[#8E9299] mb-1">最优参数</p>
              <p className="text-sm text-white font-mono">
                {JSON.stringify(result.best.params)}
              </p>
              <p className="text-xs text-[#8E9299] font-mono mt-1">
                Sharpe: {result.best.sharpe_ratio.toFixed(3)} · Return:{" "}
                {(result.best.total_return * 100).toFixed(2)}%
              </p>
            </div>

            {/* 结果列表 */}
            <div className="flex flex-col gap-1.5 max-h-80 overflow-y-auto custom-scrollbar">
              {result.results.map((r, i) => (
                <div
                  key={i}
                  className={cn(
                    "flex justify-between items-center px-4 py-2.5 rounded-lg text-xs font-mono",
                    i === 0
                      ? "bg-blue-600/20 border border-blue-600/30"
                      : "bg-[#1C1E22]",
                  )}
                >
                  <span className="text-[#8E9299]">{JSON.stringify(r.params)}</span>
                  <span className="text-white font-bold">
                    {r.sharpe_ratio.toFixed(3)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        ) : (
          <div className="flex items-center justify-center h-full text-[#4A4D55] text-sm">
            配置参数后点击「网格搜索」
          </div>
        )}
      </div>
    </div>
  );
}

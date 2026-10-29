/**
 * ScreenerPanel — Stock screening UI with 4 sub-tabs.
 *
 * Sub-tabs:
 *   - Results: sorted screening results table
 *   - Market: market breadth, indices, sector rankings
 *   - Detail: per-stock score breakdown + chip analysis
 *   - Analyze: 4-phase LLM streaming analysis
 */
import { useState, useEffect, useCallback } from "react";
import {
  Search,
  Play,
  RefreshCw,
  BarChart2,
  Globe,
  Layers,
  Brain,
  AlertTriangle,
  TrendingUp,
  TrendingDown,
} from "lucide-react";
import FeatureGuideButton from "@/components/guides/FeatureGuideButton";
import {
  useScreenerStore,
  type ScreenResult,
  type DecisionData,
} from "../store/screenerStore";

type SubTab = "results" | "market" | "detail" | "analyze";

const SUB_TABS: {
  key: SubTab;
  label: string;
  icon: React.ComponentType<{ size?: number; className?: string }>;
}[] = [
  { key: "results", label: "选股结果", icon: BarChart2 },
  { key: "market", label: "市场概况", icon: Globe },
  { key: "detail", label: "个股详情", icon: Layers },
  { key: "analyze", label: "AI分析", icon: Brain },
];

function cn(...classes: (string | undefined | false | null)[]): string {
  return classes.filter(Boolean).join(" ");
}

// ── Score bar ─────────────────────────────────────────────────────────────────

function ScoreBar({
  value,
  color = "#2962ff",
}: {
  value: number;
  color?: string;
}) {
  return (
    <div className="w-full h-1.5 bg-[#2a2e39] rounded-full overflow-hidden">
      <div
        className="h-full rounded-full transition-all"
        style={{ width: `${Math.max(0, Math.min(value, 100))}%`, backgroundColor: color }}
      />
    </div>
  );
}

// ── Results Table ─────────────────────────────────────────────────────────────

function ResultsTable() {
  const { screenResults, selectedSymbol, setSelectedSymbol } = useScreenerStore();

  if (!screenResults.length) {
    return <EmptyState label='输入股票代码后点击"开始选股"' />;
  }

  const scoreColor = (s: number) =>
    s >= 70 ? "#26a69a" : s >= 50 ? "#f9a825" : "#ef5350";

  return (
    <div className="overflow-auto h-full">
      <table className="w-full text-xs">
        <thead className="sticky top-0 bg-[#1a1e2e] z-10">
          <tr className="text-[#8b949e] border-b border-[#2a2e39]">
            {["代码", "总分", "趋势", "量能", "MACD", "RSI", "匹配"].map((h) => (
              <th key={h} className="px-3 py-2 text-left font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {screenResults.map((r) => (
            <tr
              key={r.symbol}
              onClick={() => setSelectedSymbol(r.symbol)}
              className={cn(
                "border-b border-[#2a2e39] cursor-pointer transition-colors",
                selectedSymbol === r.symbol
                  ? "bg-[#2962ff]/10"
                  : "hover:bg-[#2a2e39]/50",
              )}
            >
              <td className="px-3 py-2 font-mono text-white font-bold">{r.symbol}</td>
              <td className="px-3 py-2">
                <div className="flex items-center gap-2">
                  <span
                    className="font-mono font-bold"
                    style={{ color: scoreColor(r.score) }}
                  >
                    {r.score.toFixed(0)}
                  </span>
                  <ScoreBar value={r.score} color={scoreColor(r.score)} />
                </div>
              </td>
              <td className="px-3 py-2 font-mono text-[#c9d1d9]">{r.trend.toFixed(0)}</td>
              <td className="px-3 py-2 font-mono text-[#c9d1d9]">{r.volume.toFixed(0)}</td>
              <td className="px-3 py-2 font-mono text-[#c9d1d9]">{r.macd.toFixed(0)}</td>
              <td className="px-3 py-2 font-mono text-[#c9d1d9]">{r.rsi.toFixed(0)}</td>
              <td className="px-3 py-2">
                {r.matched ? (
                  <span className="px-1.5 py-0.5 bg-emerald-500/20 text-emerald-400 rounded text-[10px] font-bold">
                    命中
                  </span>
                ) : (
                  <span className="px-1.5 py-0.5 bg-[#2a2e39] text-[#434651] rounded text-[10px]">
                    未命中
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Market Review Panel ───────────────────────────────────────────────────────

interface MarketData {
  date: string;
  advances: number;
  declines: number;
  flat: number;
  advance_ratio: number;
  stance: "A" | "B" | "C";
  indices: { name: string; change_pct: number; close: number }[];
  top_sectors: { name: string; change_pct: number }[];
  bottom_sectors: { name: string; change_pct: number }[];
}

function MarketReviewPanel() {
  const [data, setData] = useState<MarketData | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setLoading(true);
    fetch("/api/screener/market")
      .then((r) => r.json())
      .then((d: unknown) => setData(d as MarketData))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <LoadingState label="加载市场数据..." />;
  if (!data) return <EmptyState label="市场数据加载失败" />;

  const stanceColor =
    data.stance === "A"
      ? "text-emerald-400 bg-emerald-400/10"
      : data.stance === "C"
        ? "text-red-400 bg-red-400/10"
        : "text-yellow-400 bg-yellow-400/10";
  const stanceLabel = data.stance === "A" ? "强势" : data.stance === "C" ? "弱势" : "中性";

  return (
    <div className="p-4 space-y-4 overflow-y-auto h-full">
      <div className="flex items-center gap-4">
        <span className={cn("px-3 py-1 rounded-lg text-sm font-bold", stanceColor)}>
          市场 {stanceLabel} ({data.stance})
        </span>
        <span className="text-xs text-[#8b949e]">{data.date}</span>
        <div className="ml-auto flex gap-4 text-xs">
          <span className="text-emerald-400">↑ {data.advances}</span>
          <span className="text-red-400">↓ {data.declines}</span>
          <span className="text-[#8b949e]">→ {data.flat}</span>
        </div>
      </div>

      {data.indices.length > 0 && (
        <div className="grid grid-cols-3 gap-3">
          {data.indices.map((idx) => (
            <div
              key={idx.name}
              className="bg-[#1e222d] border border-[#2a2e39] rounded-xl p-3"
            >
              <div className="text-xs text-[#8b949e] mb-1">{idx.name}</div>
              <div className="text-sm font-mono font-bold text-white">
                {idx.close.toFixed(2)}
              </div>
              <div
                className={cn(
                  "text-xs font-mono font-bold",
                  idx.change_pct >= 0 ? "text-emerald-400" : "text-red-400",
                )}
              >
                {idx.change_pct >= 0 ? "+" : ""}
                {idx.change_pct.toFixed(2)}%
              </div>
            </div>
          ))}
        </div>
      )}

      {(data.top_sectors.length > 0 || data.bottom_sectors.length > 0) && (
        <div className="grid grid-cols-2 gap-4">
          <div>
            <div className="text-xs text-[#8b949e] mb-2 flex items-center gap-1">
              <TrendingUp size={11} className="text-emerald-400" /> 领涨板块
            </div>
            {data.top_sectors.map((s) => (
              <div key={s.name} className="flex justify-between items-center py-1 text-xs">
                <span className="text-[#c9d1d9]">{s.name}</span>
                <span className="font-mono text-emerald-400">
                  +{s.change_pct.toFixed(2)}%
                </span>
              </div>
            ))}
          </div>
          <div>
            <div className="text-xs text-[#8b949e] mb-2 flex items-center gap-1">
              <TrendingDown size={11} className="text-red-400" /> 领跌板块
            </div>
            {data.bottom_sectors.map((s) => (
              <div key={s.name} className="flex justify-between items-center py-1 text-xs">
                <span className="text-[#c9d1d9]">{s.name}</span>
                <span className="font-mono text-red-400">{s.change_pct.toFixed(2)}%</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Score Detail Panel ────────────────────────────────────────────────────────

interface ScoreDetailData {
  score: {
    total: number;
    trend: number;
    bias: number;
    volume: number;
    support: number;
    macd: number;
    rsi: number;
    details: Record<string, number>;
  };
  chip: {
    avg_cost: number;
    profit_ratio: number;
    concentration: number;
    support_levels: number[];
  };
}

function ScoreDetailPanel() {
  const { selectedSymbol, lookbackDays } = useScreenerStore();
  const [data, setData] = useState<ScoreDetailData | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!selectedSymbol) return;
    setLoading(true);
    fetch(
      `/api/screener/score/${encodeURIComponent(selectedSymbol)}?lookback_days=${lookbackDays}`,
    )
      .then((r) => r.json())
      .then((d: unknown) => setData(d as ScoreDetailData))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [selectedSymbol, lookbackDays]);

  if (!selectedSymbol) return <EmptyState label="在选股结果中点击股票查看详情" />;
  if (loading) return <LoadingState label={`加载 ${selectedSymbol} 详情...`} />;
  if (!data) return <EmptyState label="加载失败" />;

  const { score, chip } = data;
  const factors = [
    { label: "趋势", value: score.trend, weight: "30%", color: "#2962ff" },
    { label: "偏离", value: score.bias, weight: "20%", color: "#9c27b0" },
    { label: "量能", value: score.volume, weight: "15%", color: "#f9a825" },
    { label: "支撑", value: score.support, weight: "10%", color: "#26a69a" },
    { label: "MACD", value: score.macd, weight: "15%", color: "#ef5350" },
    { label: "RSI", value: score.rsi, weight: "10%", color: "#ff9800" },
  ];
  const totalColor =
    score.total >= 70 ? "#26a69a" : score.total >= 50 ? "#f9a825" : "#ef5350";

  return (
    <div className="p-4 space-y-4 overflow-y-auto h-full">
      <div className="bg-[#1e222d] border border-[#2a2e39] rounded-xl p-4 flex items-center gap-4">
        <div>
          <div className="text-xs text-[#8b949e]">综合评分</div>
          <div className="text-3xl font-black" style={{ color: totalColor }}>
            {score.total.toFixed(0)}
          </div>
        </div>
        <div className="flex-1">
          <ScoreBar value={score.total} color={totalColor} />
        </div>
      </div>

      <div className="bg-[#1e222d] border border-[#2a2e39] rounded-xl p-4 space-y-3">
        <div className="text-xs text-[#8b949e] font-bold uppercase tracking-wider mb-1">
          因子分解
        </div>
        {factors.map((f) => (
          <div key={f.label} className="space-y-1">
            <div className="flex justify-between text-xs">
              <span className="text-[#c9d1d9]">
                {f.label}{" "}
                <span className="text-[#434651]">({f.weight})</span>
              </span>
              <span className="font-mono font-bold" style={{ color: f.color }}>
                {f.value.toFixed(0)}
              </span>
            </div>
            <ScoreBar value={f.value} color={f.color} />
          </div>
        ))}
      </div>

      <div className="bg-[#1e222d] border border-[#2a2e39] rounded-xl p-4 space-y-2">
        <div className="text-xs text-[#8b949e] font-bold uppercase tracking-wider mb-1">
          筹码结构
        </div>
        {(
          [
            ["平均成本", `¥${chip.avg_cost}`],
            ["盈利比例", `${(chip.profit_ratio * 100).toFixed(1)}%`],
            ["筹码集中度", `${(chip.concentration * 100).toFixed(1)}%`],
          ] as [string, string][]
        ).map(([k, v]) => (
          <div key={k} className="flex justify-between text-xs">
            <span className="text-[#8b949e]">{k}</span>
            <span className="text-white font-mono">{v}</span>
          </div>
        ))}
        {chip.support_levels.length > 0 && (
          <div className="pt-2 border-t border-[#2a2e39]">
            <div className="text-[10px] text-[#8b949e] mb-1">支撑位</div>
            <div className="flex gap-2 flex-wrap">
              {chip.support_levels.map((l) => (
                <span
                  key={l}
                  className="text-[10px] font-mono text-emerald-400 bg-emerald-400/10 px-1.5 py-0.5 rounded"
                >
                  {l.toFixed(2)}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── LLM Analyze Panel ─────────────────────────────────────────────────────────

const PHASE_LABELS = ["市场数据", "技术指标", "情报收集", "投资建议"];
const PHASE_COLORS = [
  "text-blue-400",
  "text-purple-400",
  "text-yellow-400",
  "text-emerald-400",
];

interface PhaseEvent {
  phase: 1 | 2 | 3 | 4;
  title?: string;
  token?: string;
  start?: boolean;
  done?: boolean;
  decision?: DecisionData;
}

function AnalyzePanel() {
  const {
    selectedSymbol,
    phaseContents,
    phaseStatus,
    analyzeDecision,
    analyzeLoading,
    resetAnalysis,
    appendPhaseToken,
    setPhaseStatus,
    setAnalyzeDecision,
    setAnalyzeLoading,
  } = useScreenerStore();

  const runAnalysis = useCallback(async () => {
    if (!selectedSymbol) return;
    resetAnalysis();
    setAnalyzeLoading(true);
    try {
      const res = await fetch(
        `/api/screener/analyze/${encodeURIComponent(selectedSymbol)}`,
        { method: "POST" },
      );
      if (!res.ok || !res.body) return;
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        for (const line of decoder.decode(value).split("\n")) {
          if (!line.startsWith("data: ")) continue;
          const payload = line.slice(6).trim();
          if (payload === "[DONE]") break;
          try {
            const evt = JSON.parse(payload) as PhaseEvent;
            if (evt.start) setPhaseStatus(evt.phase, "running");
            if (evt.token) appendPhaseToken(evt.phase, evt.token);
            if (evt.done) {
              setPhaseStatus(evt.phase, "done");
              if (evt.decision) setAnalyzeDecision(evt.decision);
            }
          } catch {
            /* skip malformed SSE chunk */
          }
        }
      }
    } catch {
      /* noop */
    } finally {
      setAnalyzeLoading(false);
    }
  }, [
    selectedSymbol,
    resetAnalysis,
    setAnalyzeLoading,
    setPhaseStatus,
    appendPhaseToken,
    setAnalyzeDecision,
  ]);

  if (!selectedSymbol) return <EmptyState label="在选股结果中选择股票后运行分析" />;

  const recColor =
    analyzeDecision?.recommendation === "BUY"
      ? "text-emerald-400"
      : analyzeDecision?.recommendation === "SELL"
        ? "text-red-400"
        : "text-yellow-400";

  return (
    <div className="flex flex-col h-full p-4 gap-4">
      <div className="flex items-center gap-3 shrink-0">
        <span className="text-sm font-bold text-white">{selectedSymbol} · AI深度分析</span>
        <button
          onClick={() => void runAnalysis()}
          disabled={analyzeLoading}
          className="ml-auto flex items-center gap-2 px-4 py-2 bg-[#2962ff] hover:bg-[#2962ff]/90 disabled:opacity-50 rounded-lg text-white text-xs font-bold transition-all"
        >
          {analyzeLoading ? (
            <RefreshCw size={12} className="animate-spin" />
          ) : (
            <Brain size={12} />
          )}
          {analyzeLoading ? "分析中..." : "开始分析"}
        </button>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto space-y-3">
        {([1, 2, 3, 4] as const).map((phase) => (
          <div
            key={phase}
            className={cn(
              "bg-[#1e222d] border rounded-xl overflow-hidden transition-all",
              phaseStatus[phase] === "running"
                ? "border-[#2962ff]/50"
                : phaseStatus[phase] === "done"
                  ? "border-[#2a2e39]"
                  : "border-[#2a2e39] opacity-40",
            )}
          >
            <div className="flex items-center gap-2 px-4 py-2 border-b border-[#2a2e39]">
              <div className={cn("text-xs font-bold", PHASE_COLORS[phase - 1])}>
                Phase {phase}
              </div>
              <div className="text-xs text-[#8b949e]">{PHASE_LABELS[phase - 1]}</div>
              {phaseStatus[phase] === "running" && (
                <RefreshCw size={10} className="ml-auto animate-spin text-[#2962ff]" />
              )}
              {phaseStatus[phase] === "done" && (
                <span className="ml-auto text-[10px] text-emerald-400">完成</span>
              )}
            </div>
            {phaseContents[phase] && (
              <div className="px-4 py-3 text-xs text-[#c9d1d9] leading-relaxed whitespace-pre-wrap">
                {phaseContents[phase]}
              </div>
            )}
          </div>
        ))}

        {analyzeDecision && (
          <div className="bg-[#1e222d] border border-[#2962ff]/30 rounded-xl p-4 space-y-3">
            <div className="text-xs font-bold uppercase tracking-wider text-[#8b949e]">
              投资决策
            </div>
            <div className="flex items-center gap-4">
              <div className={cn("text-3xl font-black", recColor)}>
                {analyzeDecision.recommendation}
              </div>
              <div>
                <div className="text-xs text-[#8b949e]">置信度</div>
                <div className="text-lg font-bold text-white">
                  {analyzeDecision.conviction.toFixed(0)}%
                </div>
              </div>
              {analyzeDecision.buy_price !== undefined && (
                <div>
                  <div className="text-xs text-[#8b949e]">买入价</div>
                  <div className="text-sm font-bold text-emerald-400">
                    ¥{analyzeDecision.buy_price}
                  </div>
                </div>
              )}
              {analyzeDecision.stop_loss !== undefined && (
                <div>
                  <div className="text-xs text-[#8b949e]">止损价</div>
                  <div className="text-sm font-bold text-red-400">
                    ¥{analyzeDecision.stop_loss}
                  </div>
                </div>
              )}
            </div>
            {analyzeDecision.checklist.length > 0 && (
              <div className="space-y-1">
                <div className="text-[10px] text-[#8b949e] font-bold uppercase">
                  操作核查清单
                </div>
                {analyzeDecision.checklist.map((item, i) => (
                  <div key={i} className="flex items-start gap-2 text-xs text-[#c9d1d9]">
                    <span className="text-[#2962ff] shrink-0">·</span>
                    {item}
                  </div>
                ))}
              </div>
            )}
            {analyzeDecision.summary && (
              <div className="text-xs text-[#8b949e] pt-2 border-t border-[#2a2e39]">
                {analyzeDecision.summary}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Utility ───────────────────────────────────────────────────────────────────

function LoadingState({ label }: { label: string }) {
  return (
    <div className="flex items-center justify-center h-40 gap-2 text-[#8b949e] text-sm">
      <RefreshCw size={14} className="animate-spin" />
      {label}
    </div>
  );
}

function EmptyState({ label }: { label: string }) {
  return (
    <div className="flex items-center justify-center h-40 text-[#434651] text-sm">
      {label}
    </div>
  );
}

// ── Main ──────────────────────────────────────────────────────────────────────

export default function ScreenerPanel() {
  const [subTab, setSubTab] = useState<SubTab>("results");
  const {
    strategies,
    selectedStrategyId,
    inputSymbols,
    lookbackDays,
    screenLoading,
    screenError,
    totalScreened,
    matchedCount,
    setStrategies,
    setSelectedStrategy,
    setInputSymbols,
    setLookbackDays,
    setScreenResults,
    setScreenLoading,
    setScreenError,
  } = useScreenerStore();

  useEffect(() => {
    fetch("/api/screener/strategies")
      .then((r) => r.json())
      .then((d: unknown) => {
        const data = d as { strategies: typeof strategies };
        setStrategies(data.strategies);
      })
      .catch(() => {});
  }, [setStrategies]);

  const runScreen = async () => {
    const symbols = inputSymbols
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    if (!symbols.length) return;
    setScreenLoading(true);
    setScreenError(null);
    try {
      const res = await fetch("/api/screener/screen", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          strategy_id: selectedStrategyId,
          symbols,
          lookback_days: lookbackDays,
        }),
      });
      if (!res.ok) {
        setScreenError("选股请求失败");
        return;
      }
      const d = (await res.json()) as {
        results: ScreenResult[];
        total_screened: number;
        matched_count: number;
      };
      setScreenResults(d.results, d.total_screened, d.matched_count);
      setSubTab("results");
    } catch (e) {
      setScreenError(String(e));
    } finally {
      setScreenLoading(false);
    }
  };
  const guideKey =
    subTab === "results"
      ? "screener.results"
      : subTab === "market"
        ? "screener.market"
        : subTab === "detail"
          ? "screener.detail"
          : "screener.analyze";

  return (
    <div
      className="relative flex overflow-hidden bg-[#131722]"
      style={{ height: "calc(100vh - 130px)" }}
    >
      {/* Sidebar */}
      <aside className="w-64 shrink-0 border-r border-[#2a2e39] bg-[#1a1e2e] flex flex-col p-4 gap-4 overflow-y-auto">
        <div className="text-sm font-semibold text-white flex items-center gap-2">
          <Search size={15} className="text-[#2962ff]" />
          选股配置
        </div>

        <div className="space-y-1.5">
          <label className="text-[10px] text-[#8b949e] uppercase tracking-wider font-bold">
            选股策略
          </label>
          <select
            value={selectedStrategyId}
            onChange={(e) => setSelectedStrategy(e.target.value)}
            className="w-full bg-[#2a2e39] border border-[#363a45] rounded-lg px-3 py-2 text-xs text-white outline-none focus:border-[#2962ff] transition-colors"
          >
            {strategies.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name_zh}
              </option>
            ))}
          </select>
          {strategies.find((s) => s.id === selectedStrategyId) && (
            <p className="text-[10px] text-[#434651] leading-snug">
              {strategies.find((s) => s.id === selectedStrategyId)!.description}
            </p>
          )}
        </div>

        <div className="space-y-1.5">
          <label className="text-[10px] text-[#8b949e] uppercase tracking-wider font-bold">
            股票代码
          </label>
          <textarea
            value={inputSymbols}
            onChange={(e) => setInputSymbols(e.target.value)}
            placeholder="逗号分隔，如 000001,600519"
            rows={4}
            className="w-full bg-[#2a2e39] border border-[#363a45] rounded-lg px-3 py-2 text-xs text-white outline-none focus:border-[#2962ff] resize-none transition-colors placeholder-[#8b949e]"
          />
          <p className="text-[10px] text-[#434651]">最多 50 只，逗号分隔</p>
        </div>

        <div className="space-y-1.5">
          <div className="flex justify-between">
            <label className="text-[10px] text-[#8b949e] uppercase tracking-wider font-bold">
              回溯天数
            </label>
            <span className="text-xs font-mono text-[#2962ff] bg-[#2962ff]/10 px-1.5 py-0.5 rounded">
              {lookbackDays}d
            </span>
          </div>
          <input
            type="range"
            min={60}
            max={250}
            step={10}
            value={lookbackDays}
            onChange={(e) => setLookbackDays(Number(e.target.value))}
            className="w-full accent-[#2962ff]"
          />
        </div>

        {totalScreened > 0 && (
          <div className="border-t border-[#2a2e39] pt-3 space-y-1.5">
            {(
              [
                ["已筛选", `${totalScreened} 只`],
                ["命中", `${matchedCount} 只`],
              ] as [string, string][]
            ).map(([k, v]) => (
              <div key={k} className="flex justify-between text-xs">
                <span className="text-[#8b949e]">{k}</span>
                <span className="text-white font-mono">{v}</span>
              </div>
            ))}
          </div>
        )}

        {screenError && (
          <div className="flex items-center gap-2 text-xs text-red-400 bg-red-400/10 rounded-lg p-2">
            <AlertTriangle size={12} />
            {screenError}
          </div>
        )}

        <div className="mt-auto">
          <button
            onClick={() => void runScreen()}
            disabled={screenLoading}
            className="w-full flex items-center justify-center gap-2 py-2.5 bg-[#2962ff] hover:bg-[#2962ff]/90 disabled:opacity-50 text-white text-xs font-bold rounded-lg shadow-lg shadow-[#2962ff]/20 transition-all"
          >
            {screenLoading ? (
              <RefreshCw size={13} className="animate-spin" />
            ) : (
              <Play size={13} />
            )}
            {screenLoading ? "筛选中..." : "开始选股"}
          </button>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 min-w-0 flex flex-col">
        <div className="flex items-center gap-1 px-4 py-2 border-b border-[#2a2e39] shrink-0">
          {SUB_TABS.map((t) => (
            <button
              key={t.key}
              onClick={() => setSubTab(t.key)}
              className={cn(
                "flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-all",
                subTab === t.key
                  ? "bg-[#2a2e39] text-white"
                  : "text-[#8b949e] hover:text-white hover:bg-[#2a2e39]/50",
              )}
            >
              <t.icon size={12} />
              {t.label}
            </button>
          ))}
        </div>

        <div className="flex-1 min-h-0 overflow-hidden">
          {subTab === "results" && <ResultsTable />}
          {subTab === "market" && <MarketReviewPanel />}
          {subTab === "detail" && <ScoreDetailPanel />}
          {subTab === "analyze" && <AnalyzePanel />}
        </div>
      </main>
      <FeatureGuideButton guideKey={guideKey} className="bottom-5 right-5" />
    </div>
  );
}

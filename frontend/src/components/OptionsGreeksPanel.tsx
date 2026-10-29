/**
 * 期权定价面板 — 仿 quantpilot-options-pricing 设计.
 * 功能：Black-Scholes Greeks + IV 反推 + 盈亏图 + Greeks 敏感度曲线 + 情景矩阵
 */
import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  BarChart3,
  Calculator,
  Clock,
  Info,
  Percent,
  RefreshCw,
  TrendingUp,
  Zap,
} from "lucide-react";
import FeatureGuideButton from "@/components/guides/FeatureGuideButton";
import {
  AreaChart,
  Area,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
} from "recharts";
import { cn } from "../lib/utils";

// ── 类型 ──────────────────────────────────────────────────────────────────────

interface GreeksResult {
  price: number;
  delta: number;
  gamma: number;
  theta: number;
  vega: number;
  rho: number;
  intrinsic: number;
  time_value: number;
}

interface SensPoint {
  S: number;
  price: number;
  delta: number;
  gamma: number;
  theta: number;
  vega: number;
}

interface ScenarioData {
  S_labels: number[];
  sigma_labels: number[];
  matrix: number[][];
}

// ── 子组件 ────────────────────────────────────────────────────────────────────

function InputField({
  label,
  value,
  onChange,
  step = "0.01",
  icon: Icon,
  suffix,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  step?: string;
  icon?: React.ElementType;
  suffix?: string;
}) {
  return (
    <div className="flex flex-col gap-1.5 mb-3">
      <label className="text-[11px] font-medium text-[#8b949e] uppercase tracking-wider flex items-center gap-1.5">
        {Icon && <Icon size={11} className="text-[#8b949e]/70" />}
        {label}
      </label>
      <div className="relative">
        <input
          type="number"
          step={step}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="w-full bg-[#2a2e39] border border-[#363a45] rounded-md px-3 py-2 text-sm text-[#e1e4e8] outline-none focus:border-[#2962ff] hover:border-[#434651] transition-colors"
        />
        {suffix && (
          <span className="absolute right-3 top-1/2 -translate-y-1/2 text-[10px] font-bold text-[#8b949e]">
            {suffix}
          </span>
        )}
      </div>
    </div>
  );
}

const GREEK_META = [
  { key: "delta", label: "Delta",  symbol: "δ", desc: "股价变动 ¥1 时期权价格的变动量" },
  { key: "gamma", label: "Gamma",  symbol: "γ", desc: "Delta 本身的变动速度（凸性）" },
  { key: "theta", label: "Theta",  symbol: "θ", desc: "时间流逝每日对期权价格的影响" },
  { key: "vega",  label: "Vega",   symbol: "ν", desc: "波动率变化 1% 对期权价格的影响" },
  { key: "rho",   label: "Rho",    symbol: "ρ", desc: "无风险利率变化 1% 对期权价格的影响" },
] as const;

function GreekCard({
  label,
  symbol,
  value,
  desc,
}: {
  label: string;
  symbol: string;
  value: number;
  desc: string;
}) {
  return (
    <div className="bg-[#1e222d] border border-[#2a2e39] rounded-lg p-4 flex flex-col gap-1 hover:border-[#363a45] transition-all group">
      <div className="flex justify-between items-start">
        <span className="text-[11px] font-bold text-[#8b949e] uppercase tracking-widest flex items-center gap-1">
          {label}
          <span className="text-[#4a4d55] font-normal ml-0.5">({symbol})</span>
        </span>
        <div className="opacity-0 group-hover:opacity-100 transition-opacity cursor-help" title={desc}>
          <Info size={12} className="text-[#8b949e]" />
        </div>
      </div>
      <div className="text-2xl font-mono font-medium text-[#e1e4e8] mt-1">
        {value.toFixed(4)}
      </div>
      <div className="text-[10px] text-[#8b949e] leading-tight mt-1 line-clamp-2">
        {desc}
      </div>
    </div>
  );
}

// Scenario heatmap cell color
function scenarioColor(val: number, min: number, max: number): string {
  const t = max === min ? 0.5 : (val - min) / (max - min);
  const r = Math.round(41 + t * (0 - 41));
  const g = Math.round(192 * t);
  const b = Math.round(135 * t);
  return `rgba(${r},${g+20},${b+20},${0.15 + t * 0.5})`;
}

// ── 主组件 ────────────────────────────────────────────────────────────────────

type ViewTab = "payoff" | "sensitivity" | "scenario";

const VIEW_TABS: { key: ViewTab; label: string }[] = [
  { key: "payoff",      label: "盈亏图" },
  { key: "sensitivity", label: "Greeks 曲线" },
  { key: "scenario",    label: "情景矩阵" },
];

type SensMetric = "price" | "delta" | "gamma" | "vega";
const SENS_METRICS: { key: SensMetric; label: string; color: string }[] = [
  { key: "price", label: "价格",  color: "#2962ff" },
  { key: "delta", label: "Delta", color: "#00C087" },
  { key: "gamma", label: "Gamma", color: "#ff9800" },
  { key: "vega",  label: "Vega",  color: "#a855f7" },
];

export default function OptionsGreeksPanel() {
  // ── 输入状态
  const [S,     setS]     = useState("100");
  const [K,     setK]     = useState("100");
  const [T,     setT]     = useState("1");
  const [r,     setR]     = useState("0.05");
  const [sigma, setSigma] = useState("0.20");
  const [optType, setOptType] = useState<"call" | "put">("call");
  const [marketPrice, setMarketPrice] = useState("");

  // ── 结果状态
  const [greeks,       setGreeks]       = useState<GreeksResult | null>(null);
  const [iv,           setIv]           = useState<number | null>(null);
  const [sensData,     setSensData]     = useState<SensPoint[]>([]);
  const [scenario,     setScenario]     = useState<ScenarioData | null>(null);
  const [loading,      setLoading]      = useState(false);
  const [ivLoading,    setIvLoading]    = useState(false);
  const [sensLoading,  setSensLoading]  = useState(false);
  const [scenLoading,  setScenLoading]  = useState(false);

  // ── UI 状态
  const [activeView,  setActiveView]  = useState<ViewTab>("payoff");
  const [sensMetric,  setSensMetric]  = useState<SensMetric>("price");

  // ── 计算 Greeks
  const computeGreeks = async () => {
    setLoading(true);
    try {
      const res = await fetch("/api/options/greeks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          S: parseFloat(S), K: parseFloat(K),
          T: parseFloat(T), r: parseFloat(r),
          sigma: parseFloat(sigma), option_type: optType,
        }),
      });
      if (res.ok) setGreeks((await res.json()) as GreeksResult);
    } catch { /* silent */ }
    finally { setLoading(false); }
  };

  // ── 计算 IV
  const computeIV = async () => {
    if (!marketPrice) return;
    setIvLoading(true);
    try {
      const res = await fetch("/api/options/implied-vol", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          market_price: parseFloat(marketPrice),
          S: parseFloat(S), K: parseFloat(K),
          T: parseFloat(T), r: parseFloat(r),
          option_type: optType,
        }),
      });
      if (res.ok) {
        const d = (await res.json()) as { implied_vol_pct: number };
        setIv(d.implied_vol_pct);
      }
    } catch { /* silent */ }
    finally { setIvLoading(false); }
  };

  // ── 加载敏感度数据
  const loadSensitivity = async () => {
    setSensLoading(true);
    try {
      const res = await fetch("/api/options/sensitivity", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          K: parseFloat(K), T: parseFloat(T),
          r: parseFloat(r), sigma: parseFloat(sigma),
          option_type: optType, S_center: parseFloat(S),
          points: 60,
        }),
      });
      if (res.ok) {
        const d = (await res.json()) as { data: SensPoint[] };
        setSensData(d.data);
      }
    } catch { /* silent */ }
    finally { setSensLoading(false); }
  };

  // ── 加载情景矩阵
  const loadScenario = async () => {
    setScenLoading(true);
    try {
      const res = await fetch("/api/options/scenario", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          K: parseFloat(K), T: parseFloat(T),
          r: parseFloat(r), option_type: optType,
          S_center: parseFloat(S), sigma_center: parseFloat(sigma),
        }),
      });
      if (res.ok) setScenario((await res.json()) as ScenarioData);
    } catch { /* silent */ }
    finally { setScenLoading(false); }
  };

  // 初始计算
  useEffect(() => { void computeGreeks(); }, []); // eslint-disable-line

  // 切换到敏感度/情景时懒加载
  useEffect(() => {
    if (activeView === "sensitivity" && sensData.length === 0) void loadSensitivity();
    if (activeView === "scenario"    && scenario === null)      void loadScenario();
  }, [activeView]); // eslint-disable-line

  // ── 盈亏曲线（客户端计算）
  const payoffData = useMemo(() => {
    if (!greeks) return [];
    const sNum = parseFloat(S);
    const kNum = parseFloat(K);
    const range = sNum * 0.5;
    const start = Math.max(0.01, sNum - range);
    const end   = sNum + range;
    const pts   = 60;
    const step  = (end - start) / pts;
    return Array.from({ length: pts + 1 }, (_, i) => {
      const x = start + i * step;
      const intrinsic = optType === "call"
        ? Math.max(0, x - kNum)
        : Math.max(0, kNum - x);
      return { underlying: x, payoff: intrinsic, profit: intrinsic - greeks.price };
    });
  }, [S, K, optType, greeks]);

  // ── 情景矩阵 min/max
  const [scMin, scMax] = useMemo(() => {
    if (!scenario) return [0, 1];
    const flat = scenario.matrix.flat();
    return [Math.min(...flat), Math.max(...flat)];
  }, [scenario]);
  const guideKey = activeView === "payoff"
    ? "options.payoff"
    : activeView === "sensitivity"
      ? "options.sensitivity"
      : "options.scenario";

  // ── 汇总
  const hasResult = greeks !== null;
  const sNum = parseFloat(S);
  const kNum = parseFloat(K);
  const moneyness = sNum > kNum ? "价内" : sNum < kNum ? "价外" : "平值";

  return (
    <div
      className="relative flex bg-[#131722] rounded-xl overflow-hidden border border-[#2a2e39]"
      style={{ height: "calc(100vh - 120px)" }}
    >
      {/* ── 左侧参数栏 ──────────────────────────────── */}
      <aside className="w-72 shrink-0 border-r border-[#2a2e39] overflow-y-auto custom-scrollbar p-5 bg-[#131722]">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-sm font-bold text-white uppercase tracking-wider">期权参数</h2>
          <button
            onClick={() => void computeGreeks()}
            className="p-1.5 hover:bg-[#1e222d] rounded-md text-[#2962ff] transition-transform active:rotate-180 duration-500"
            title="重新计算"
          >
            <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
          </button>
        </div>

        <InputField label="标的现价 (S)"    value={S}     onChange={setS}     icon={Activity}   step="0.5" />
        <InputField label="行权价 (K)"      value={K}     onChange={setK}     icon={TrendingUp}  step="0.5" />
        <InputField label="到期时间 (年)"   value={T}     onChange={setT}     icon={Clock}       step="0.01" />
        <InputField label="无风险利率 (r)"  value={r}     onChange={setR}     icon={Percent}     step="0.001" />
        <InputField label="波动率 (σ)"      value={sigma} onChange={setSigma} icon={Zap}         step="0.01" suffix="σ" />

        {/* Call / Put 切换 */}
        <div className="flex flex-col gap-1.5 mb-5">
          <label className="text-[11px] font-medium text-[#8b949e] uppercase tracking-wider">
            期权类型
          </label>
          <div className="grid grid-cols-2 gap-2">
            <button
              onClick={() => setOptType("call")}
              className={cn(
                "py-2 text-xs font-bold rounded-md border transition-all",
                optType === "call"
                  ? "bg-[#2962ff] border-[#2962ff] text-white shadow-[0_0_15px_rgba(41,98,255,0.3)]"
                  : "bg-[#1e222d] border-[#2a2e39] text-[#8b949e] hover:border-[#363a45]",
              )}
            >
              看涨 (Call)
            </button>
            <button
              onClick={() => setOptType("put")}
              className={cn(
                "py-2 text-xs font-bold rounded-md border transition-all",
                optType === "put"
                  ? "bg-[#f23645] border-[#f23645] text-white shadow-[0_0_15px_rgba(242,54,69,0.3)]"
                  : "bg-[#1e222d] border-[#2a2e39] text-[#8b949e] hover:border-[#363a45]",
              )}
            >
              看跌 (Put)
            </button>
          </div>
        </div>

        {/* 计算按钮 */}
        <button
          onClick={() => void computeGreeks()}
          disabled={loading}
          className="w-full bg-[#2962ff] hover:bg-[#1e53e5] text-white font-bold py-2.5 rounded-md text-sm transition-all flex items-center justify-center gap-2 disabled:opacity-50"
        >
          {loading
            ? <RefreshCw size={15} className="animate-spin" />
            : <Calculator size={15} />}
          计算 Greeks
        </button>

        {/* ── IV 反推 ── */}
        <div className="mt-7 pt-7 border-t border-[#2a2e39]">
          <h3 className="text-[11px] font-bold text-[#8b949e] uppercase tracking-widest mb-4">
            隐含波动率反推
          </h3>
          <InputField label="市场价格" value={marketPrice} onChange={setMarketPrice} icon={BarChart3} />
          <button
            onClick={() => void computeIV()}
            disabled={ivLoading || !marketPrice}
            className="w-full bg-[#ff9800] hover:bg-[#e68a00] text-black font-bold py-2 rounded-md text-xs transition-all disabled:opacity-50"
          >
            {ivLoading ? "计算中…" : "求 IV"}
          </button>
          {iv !== null && (
            <div className="mt-4 p-3 bg-[#1e222d] border border-[#ff9800]/30 rounded-md">
              <div className="text-[10px] text-[#8b949e] uppercase font-bold">隐含波动率 (IV)</div>
              <div className="text-xl font-mono font-bold text-[#ff9800] mt-0.5">
                {iv.toFixed(2)}%
              </div>
            </div>
          )}
        </div>

        {/* ── 敏感度/情景刷新 ── */}
        <div className="mt-6 flex flex-col gap-2">
          <button
            onClick={() => { void loadSensitivity(); void loadScenario(); }}
            disabled={sensLoading || scenLoading}
            className="w-full py-2 border border-[#2a2e39] hover:border-[#363a45] text-[#8b949e] hover:text-white rounded-md text-xs transition-colors flex items-center justify-center gap-2"
          >
            <RefreshCw size={12} className={(sensLoading || scenLoading) ? "animate-spin" : ""} />
            刷新曲线 / 矩阵
          </button>
        </div>
      </aside>

      {/* ── 主内容区 ──────────────────────────────────── */}
      <main className="flex-1 overflow-y-auto custom-scrollbar p-6 bg-[#131722]">
        {hasResult ? (
          <div className="max-w-5xl mx-auto space-y-6">

            {/* ── 理论价格 header ── */}
            <div className="flex flex-col sm:flex-row sm:items-end justify-between gap-4 bg-[#1e222d] border border-[#2a2e39] rounded-xl p-6">
              <div>
                <div className="text-[11px] font-bold text-[#8b949e] uppercase tracking-[0.2em] mb-1">
                  理论价格 (Black-Scholes)
                </div>
                <div className="flex items-baseline gap-3">
                  <span className="text-5xl font-mono font-bold text-white tracking-tighter">
                    {greeks.price.toFixed(4)}
                  </span>
                  <span
                    className={cn(
                      "text-sm font-bold px-2 py-0.5 rounded",
                      optType === "call"
                        ? "bg-[#2962ff]/10 text-[#2962ff]"
                        : "bg-[#f23645]/10 text-[#f23645]",
                    )}
                  >
                    {optType.toUpperCase()}
                  </span>
                  <span className="text-xs font-mono text-[#8b949e] px-2 py-0.5 bg-[#2a2e39] rounded">
                    {moneyness}
                  </span>
                </div>
              </div>

              <div className="flex gap-8 text-right shrink-0">
                <div>
                  <div className="text-[10px] text-[#8b949e] uppercase font-bold tracking-widest">内在价值</div>
                  <div className="text-lg font-mono text-[#e1e4e8] mt-0.5">
                    {greeks.intrinsic.toFixed(4)}
                  </div>
                </div>
                <div>
                  <div className="text-[10px] text-[#8b949e] uppercase font-bold tracking-widest">时间价值</div>
                  <div className="text-lg font-mono text-[#e1e4e8] mt-0.5">
                    {greeks.time_value.toFixed(4)}
                  </div>
                </div>
                <div>
                  <div className="text-[10px] text-[#8b949e] uppercase font-bold tracking-widest">S / K</div>
                  <div className="text-lg font-mono text-[#e1e4e8] mt-0.5">
                    {parseFloat(S)} / {parseFloat(K)}
                  </div>
                </div>
              </div>
            </div>

            {/* ── Greeks 网格 ── */}
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
              {GREEK_META.map(({ key, label, symbol, desc }) => (
                <GreekCard
                  key={key}
                  label={label}
                  symbol={symbol}
                  value={greeks[key as keyof GreeksResult] as number}
                  desc={desc}
                />
              ))}
            </div>

            {/* ── 图表区 (三标签) ── */}
            <div className="bg-[#1e222d] border border-[#2a2e39] rounded-xl overflow-hidden">
              {/* 标签栏 */}
              <div className="flex border-b border-[#2a2e39]">
                {VIEW_TABS.map((tab) => (
                  <button
                    key={tab.key}
                    onClick={() => setActiveView(tab.key)}
                    className={cn(
                      "px-5 py-3 text-xs font-bold uppercase tracking-widest transition-colors border-b-2",
                      activeView === tab.key
                        ? "text-white border-[#2962ff]"
                        : "text-[#8b949e] border-transparent hover:text-white",
                    )}
                  >
                    {tab.label}
                  </button>
                ))}
                <div className="ml-auto flex items-center gap-4 pr-5 text-[10px] font-bold uppercase tracking-widest text-[#8b949e]">
                  {activeView === "payoff" && (
                    <>
                      <div className="flex items-center gap-1.5">
                        <div className="w-3 h-0.5 bg-[#2962ff]" />
                        到期盈亏
                      </div>
                      <div className="flex items-center gap-1.5">
                        <div className="w-3 h-0.5 border-t border-dashed border-[#8b949e]" />
                        行权价
                      </div>
                    </>
                  )}
                  {activeView === "sensitivity" && (
                    <div className="flex gap-3">
                      {SENS_METRICS.map((m) => (
                        <button
                          key={m.key}
                          onClick={() => setSensMetric(m.key)}
                          className={cn(
                            "flex items-center gap-1 px-2 py-0.5 rounded transition-colors",
                            sensMetric === m.key ? "bg-[#2a2e39] text-white" : "hover:text-white",
                          )}
                          style={{ color: sensMetric === m.key ? m.color : undefined }}
                        >
                          <div className="w-2 h-2 rounded-full" style={{ background: m.color }} />
                          {m.label}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              </div>

              <div className="p-6">
                {/* 盈亏图 */}
                {activeView === "payoff" && (
                  <div className="h-[320px]">
                    <ResponsiveContainer width="100%" height="100%">
                      <AreaChart data={payoffData} margin={{ top: 10, right: 10, left: 0, bottom: 20 }}>
                        <defs>
                          <linearGradient id="colorProfit" x1="0" y1="0" x2="0" y2="1">
                            <stop offset="5%"  stopColor="#2962ff" stopOpacity={0.3} />
                            <stop offset="95%" stopColor="#2962ff" stopOpacity={0} />
                          </linearGradient>
                        </defs>
                        <CartesianGrid strokeDasharray="3 3" stroke="#2a2e39" vertical={false} />
                        <XAxis
                          dataKey="underlying"
                          stroke="#434651"
                          fontSize={10}
                          tickFormatter={(v: number) => v.toFixed(0)}
                          label={{ value: "标的价格", position: "insideBottomRight", offset: -5, fontSize: 10, fill: "#434651" }}
                        />
                        <YAxis
                          stroke="#434651"
                          fontSize={10}
                          label={{ value: "盈亏", angle: -90, position: "insideLeft", fontSize: 10, fill: "#434651" }}
                        />
                        <Tooltip
                          contentStyle={{ backgroundColor: "#1e222d", border: "1px solid #363a45", borderRadius: 4, fontSize: 11 }}
                          itemStyle={{ color: "#fff" }}
                          labelStyle={{ color: "#8b949e", marginBottom: 4 }}
                          formatter={(v: unknown) => [(v as number).toFixed(2), "盈亏"]}
                          labelFormatter={(v: unknown) => `标的: ${Number(v).toFixed(2)}`}
                        />
                        <ReferenceLine y={0}         stroke="#434651" strokeWidth={1} />
                        <ReferenceLine
                          x={parseFloat(K)}
                          stroke="#8b949e"
                          strokeDasharray="4 3"
                          label={{ value: "K", position: "top", fill: "#8b949e", fontSize: 10 }}
                        />
                        <Area
                          type="monotone"
                          dataKey="profit"
                          stroke="#2962ff"
                          strokeWidth={2}
                          fillOpacity={1}
                          fill="url(#colorProfit)"
                        />
                      </AreaChart>
                    </ResponsiveContainer>
                  </div>
                )}

                {/* Greeks 敏感度曲线 */}
                {activeView === "sensitivity" && (
                  <div className="h-[320px]">
                    {sensLoading ? (
                      <div className="h-full flex items-center justify-center text-[#8b949e] text-sm gap-2">
                        <RefreshCw size={16} className="animate-spin" />
                        加载中…
                      </div>
                    ) : sensData.length === 0 ? (
                      <div className="h-full flex items-center justify-center text-[#8b949e] text-sm">
                        请点击「刷新曲线」加载
                      </div>
                    ) : (
                      <ResponsiveContainer width="100%" height="100%">
                        <LineChart data={sensData} margin={{ top: 10, right: 10, left: 0, bottom: 20 }}>
                          <CartesianGrid strokeDasharray="3 3" stroke="#2a2e39" vertical={false} />
                          <XAxis
                            dataKey="S"
                            stroke="#434651"
                            fontSize={10}
                            tickFormatter={(v: number) => v.toFixed(0)}
                            label={{ value: "标的价格 (S)", position: "insideBottomRight", offset: -5, fontSize: 10, fill: "#434651" }}
                          />
                          <YAxis stroke="#434651" fontSize={10} />
                          <Tooltip
                            contentStyle={{ backgroundColor: "#1e222d", border: "1px solid #363a45", borderRadius: 4, fontSize: 11 }}
                            itemStyle={{ color: "#fff" }}
                            labelFormatter={(v: unknown) => `S = ${Number(v).toFixed(2)}`}
                            formatter={(v: unknown) => [(v as number).toFixed(4)]}
                          />
                          <ReferenceLine
                            x={parseFloat(S)}
                            stroke="#8b949e"
                            strokeDasharray="4 3"
                            label={{ value: "现价", position: "top", fill: "#8b949e", fontSize: 10 }}
                          />
                          <ReferenceLine
                            x={parseFloat(K)}
                            stroke="#434651"
                            strokeDasharray="4 3"
                            label={{ value: "K", position: "insideTopRight", fill: "#434651", fontSize: 10 }}
                          />
                          <Line
                            type="monotone"
                            dataKey={sensMetric}
                            stroke={SENS_METRICS.find((m) => m.key === sensMetric)?.color ?? "#2962ff"}
                            strokeWidth={2}
                            dot={false}
                            name={SENS_METRICS.find((m) => m.key === sensMetric)?.label}
                          />
                        </LineChart>
                      </ResponsiveContainer>
                    )}
                  </div>
                )}

                {/* 情景矩阵 */}
                {activeView === "scenario" && (
                  <div>
                    {scenLoading ? (
                      <div className="h-[320px] flex items-center justify-center text-[#8b949e] text-sm gap-2">
                        <RefreshCw size={16} className="animate-spin" />
                        计算中…
                      </div>
                    ) : !scenario ? (
                      <div className="h-[320px] flex items-center justify-center text-[#8b949e] text-sm">
                        请点击「刷新曲线」加载
                      </div>
                    ) : (
                      <div className="overflow-x-auto">
                        <p className="text-[11px] text-[#8b949e] mb-3 font-mono">
                          期权理论价格 — 行: 波动率(σ)，列: 标的价格(S)
                        </p>
                        <table className="w-full border-collapse text-[11px] font-mono">
                          <thead>
                            <tr>
                              <th className="px-3 py-2 text-left text-[#8b949e]">σ \ S</th>
                              {scenario.S_labels.map((s) => (
                                <th
                                  key={s}
                                  className={cn(
                                    "px-3 py-2 text-center text-[#8b949e]",
                                    Math.abs(s - parseFloat(S)) < 1 && "text-white font-bold",
                                  )}
                                >
                                  {s.toFixed(1)}
                                </th>
                              ))}
                            </tr>
                          </thead>
                          <tbody>
                            {scenario.matrix.map((row, ri) => (
                              <tr key={ri}>
                                <td className="px-3 py-2 text-[#8b949e]">
                                  {scenario.sigma_labels[ri]}%
                                </td>
                                {row.map((val, ci) => (
                                  <td
                                    key={ci}
                                    className="px-3 py-2 text-center text-[#e1e4e8] rounded transition-colors"
                                    style={{ background: scenarioColor(val, scMin, scMax) }}
                                  >
                                    {val.toFixed(2)}
                                  </td>
                                ))}
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>

            {/* ── 底部说明 ── */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-5 text-[11px] text-[#8b949e] leading-relaxed">
              <div>
                <div className="font-bold text-[#e1e4e8] uppercase tracking-widest mb-2">模型说明</div>
                <p>
                  采用标准 Black-Scholes-Merton 模型定价。假设标的价格服从几何布朗运动，波动率和无风险利率在期权有效期内保持不变。
                </p>
              </div>
              <div>
                <div className="font-bold text-[#e1e4e8] uppercase tracking-widest mb-2">风险提示</div>
                <p>
                  理论价格仅供参考，实际市场价格受供需、流动性溢价及偏斜（Skew）影响。交易衍生品具有高风险，请谨慎操作。
                </p>
              </div>
              <div>
                <div className="font-bold text-[#e1e4e8] uppercase tracking-widest mb-2">Greeks 含义</div>
                <p>
                  Δ 方向风险，Γ Delta 稳定性，Θ 时间损耗，ν 波动率敏感度，ρ 利率敏感度。
                  Vega / Rho 均为每 1% 变动的影响。
                </p>
              </div>
            </div>

          </div>
        ) : (
          /* 空状态 */
          <div className="h-full flex flex-col items-center justify-center text-[#8b949e] gap-4">
            <div className="w-16 h-16 bg-[#1e222d] rounded-full flex items-center justify-center border border-[#2a2e39]">
              <Calculator size={32} className="text-[#8b949e]/50" />
            </div>
            <div className="text-center">
              <p className="text-sm font-medium text-[#e1e4e8]">输入参数后点击「计算 Greeks」</p>
              <p className="text-xs mt-1">理论价格与风险指标将在此处实时显示</p>
            </div>
          </div>
        )}
      </main>
      <FeatureGuideButton guideKey={guideKey} className="bottom-5 right-5" />
    </div>
  );
}

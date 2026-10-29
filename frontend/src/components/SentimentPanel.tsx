/**
 * 新闻情绪分析面板.
 * 布局: 左侧配置侧栏 + 右侧主区域（情绪仪表 + 趋势图 + 新闻列表）
 */
import { useEffect, useState, useCallback } from "react";
import { motion } from "motion/react";
import {
  Settings2,
  Search,
  Info,
  MessageSquare,
  Zap,
  ExternalLink,
  RefreshCw,
  TrendingUp,
  TrendingDown,
  Minus,
} from "lucide-react";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip as RechartsTooltip,
  ResponsiveContainer,
} from "recharts";

import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Label } from "@/components/ui/label";
import { Slider } from "@/components/ui/slider";
import { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider } from "@/components/ui/tooltip";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";

// ── 数据类型 ──────────────────────────────────────────────────────────────────

interface SentimentHistoryPoint {
  timestamp: string;
  bullish: number;
  bearish: number;
  neutral: number;
  score: number;
}

interface NewsSentimentItem {
  id: string;
  title: string;
  url: string;
  published: string;
  source: string;
  description: string;
  impact: "high" | "medium" | "low";
  sentiment: {
    compound: number;
    positive: number;
    negative: number;
    neutral: number;
    label: "bullish" | "bearish" | "neutral";
  } | null;
}

interface AggregateData {
  count: number;
  avg_compound: number;
  positive_ratio: number;
  score: number;
}

const EMPTY_AGGREGATE: AggregateData = {
  count: 0, avg_compound: 0, positive_ratio: 0, score: 50,
};

// ── 子组件: SentimentGauge ─────────────────────────────────────────────────

function SentimentGauge({ score }: { score: number }) {
  const rotation = (score / 100) * 180 - 90;

  const getSentimentInfo = (s: number) => {
    if (s >= 75) return { label: "极度看涨", color: "text-emerald-400", bg: "bg-emerald-400/10", icon: TrendingUp };
    if (s >= 60) return { label: "看涨", color: "text-[#26a69a]", bg: "bg-[#26a69a]/10", icon: TrendingUp };
    if (s >= 40) return { label: "中性", color: "text-yellow-400", bg: "bg-yellow-400/10", icon: Minus };
    if (s >= 25) return { label: "看跌", color: "text-orange-400", bg: "bg-orange-400/10", icon: TrendingDown };
    return { label: "极度看跌", color: "text-[#ef5350]", bg: "bg-[#ef5350]/10", icon: TrendingDown };
  };

  const info = getSentimentInfo(score);
  const SentIcon = info.icon;

  return (
    <div className="bg-[#1e222d] border border-[#2a2e39] rounded-xl p-4 h-full flex flex-col">
      <div className="text-[10px] font-bold uppercase tracking-wider text-[#8b949e] mb-3">
        市场情绪指数
      </div>

      <div className="flex-1 flex flex-col items-center justify-center">
        {/* SVG Gauge */}
        <div className="relative w-44 h-22 overflow-hidden" style={{ height: 88 }}>
          <svg viewBox="0 0 100 50" className="w-full h-full">
            <defs>
              <linearGradient id="sentGaugeGrad" x1="0%" y1="0%" x2="100%" y2="0%">
                <stop offset="0%" stopColor="#ef5350" />
                <stop offset="50%" stopColor="#fdd835" />
                <stop offset="100%" stopColor="#26a69a" />
              </linearGradient>
            </defs>
            {/* Track */}
            <path
              d="M 10 50 A 40 40 0 0 1 90 50"
              fill="none"
              stroke="#2a2e39"
              strokeWidth="12"
              strokeLinecap="round"
            />
            {/* Colored arc */}
            <path
              d="M 10 50 A 40 40 0 0 1 90 50"
              fill="none"
              stroke="url(#sentGaugeGrad)"
              strokeWidth="12"
              strokeLinecap="round"
              opacity="0.9"
            />
          </svg>
          {/* Animated needle */}
          <motion.div
            className="absolute bottom-0 left-1/2 w-0.5 h-14 bg-white origin-bottom"
            style={{ translateX: "-50%" }}
            initial={{ rotate: -90 }}
            animate={{ rotate: rotation }}
            transition={{ type: "spring", stiffness: 60, damping: 15 }}
          >
            <div className="absolute top-0 left-1/2 -translate-x-1/2 w-3 h-3 bg-white rounded-full border-2 border-[#1e222d] shadow-lg" />
          </motion.div>
        </div>

        {/* Score display */}
        <div className="mt-3 text-center">
          <div className="text-5xl font-black tracking-tighter text-white">{score}</div>
          <div className={cn("mt-2 inline-flex items-center gap-1 px-3 py-1 rounded-full text-xs font-bold", info.bg, info.color)}>
            <SentIcon size={11} />
            {info.label}
          </div>
        </div>

        {/* Scale labels */}
        <div className="w-full mt-4 grid grid-cols-3 gap-2 text-[9px] uppercase font-bold text-[#434651] text-center px-2">
          <div>看跌</div>
          <div>中性</div>
          <div>看涨</div>
        </div>
      </div>
    </div>
  );
}

// ── 子组件: SentimentChart ────────────────────────────────────────────────

function SentimentChart({ data }: { data: SentimentHistoryPoint[] }) {
  return (
    <div className="bg-[#1e222d] border border-[#2a2e39] rounded-xl p-4 h-full flex flex-col">
      <div className="flex items-center justify-between mb-3">
        <div className="text-[10px] font-bold uppercase tracking-wider text-[#8b949e]">
          情绪趋势分析 (24H)
        </div>
        <div className="flex gap-4">
          <div className="flex items-center gap-1.5">
            <div className="w-2 h-2 rounded-full bg-[#26a69a]" />
            <span className="text-[9px] font-bold uppercase text-[#26a69a]">看涨</span>
          </div>
          <div className="flex items-center gap-1.5">
            <div className="w-2 h-2 rounded-full bg-[#ef5350]" />
            <span className="text-[9px] font-bold uppercase text-[#ef5350]">看跌</span>
          </div>
        </div>
      </div>
      <div className="flex-1 min-h-0" style={{ height: 200 }}>
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data} margin={{ top: 8, right: 8, left: -24, bottom: 0 }}>
            <defs>
              <linearGradient id="sentBullish" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="#26a69a" stopOpacity={0.3} />
                <stop offset="95%" stopColor="#26a69a" stopOpacity={0} />
              </linearGradient>
              <linearGradient id="sentBearish" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="#ef5350" stopOpacity={0.3} />
                <stop offset="95%" stopColor="#ef5350" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="rgba(255,255,255,0.04)" />
            <XAxis
              dataKey="timestamp"
              axisLine={false}
              tickLine={false}
              tick={{ fontSize: 9, fill: "#434651" }}
            />
            <YAxis
              axisLine={false}
              tickLine={false}
              tick={{ fontSize: 9, fill: "#434651" }}
            />
            <RechartsTooltip
              contentStyle={{
                backgroundColor: "#1e222d",
                border: "1px solid #363a45",
                borderRadius: 6,
                fontSize: 11,
              }}
              itemStyle={{ color: "#e0e0e0" }}
              labelStyle={{ color: "#8b949e", marginBottom: 4, fontSize: 10 }}
              formatter={(v: unknown) => [(v as number).toFixed(1) + "%"]}
            />
            <Area
              type="monotone"
              dataKey="bullish"
              stroke="#26a69a"
              strokeWidth={2}
              fillOpacity={1}
              fill="url(#sentBullish)"
              name="看涨"
            />
            <Area
              type="monotone"
              dataKey="bearish"
              stroke="#ef5350"
              strokeWidth={2}
              fillOpacity={1}
              fill="url(#sentBearish)"
              name="看跌"
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

// ── 子组件: NewsFeed ──────────────────────────────────────────────────────

function NewsFeed({ news, total }: { news: NewsSentimentItem[]; total: number }) {
  const getSentimentBadge = (label: "bullish" | "bearish" | "neutral" | undefined) => {
    if (label === "bullish")
      return (
        <Badge className="bg-[#26a69a]/10 text-[#26a69a] border border-[#26a69a]/20 hover:bg-[#26a69a]/20 rounded-full">
          看涨
        </Badge>
      );
    if (label === "bearish")
      return (
        <Badge className="bg-[#ef5350]/10 text-[#ef5350] border border-[#ef5350]/20 hover:bg-[#ef5350]/20 rounded-full">
          看跌
        </Badge>
      );
    return (
      <Badge className="bg-yellow-400/10 text-yellow-400 border border-yellow-400/20 hover:bg-yellow-400/20 rounded-full">
        中性
      </Badge>
    );
  };

  const getImpactBadge = (impact: "high" | "medium" | "low") => {
    if (impact === "high")
      return (
        <div className="flex items-center gap-1 text-orange-400 font-bold text-[10px] uppercase">
          <Zap size={10} />
          高影响
        </div>
      );
    if (impact === "medium")
      return (
        <div className="text-[#2962ff] font-bold text-[10px] uppercase">
          中影响
        </div>
      );
    return (
      <div className="text-[#8b949e] font-bold text-[10px] uppercase">
        低影响
      </div>
    );
  };

  return (
    <div className="bg-[#1e222d] border border-[#2a2e39] rounded-xl flex flex-col h-full">
      <div className="flex items-center justify-between px-4 py-3 border-b border-[#2a2e39] shrink-0">
        <div className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-wider text-[#8b949e]">
          <MessageSquare size={13} className="text-[#2962ff]" />
          最新新闻情绪分析
        </div>
        <div className="text-[10px] text-[#8b949e]">共 {total} 条结果</div>
      </div>
      <div className="flex-1 min-h-0 overflow-hidden">
        <ScrollArea className="h-full">
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead className="w-[90px]">情绪</TableHead>
                <TableHead>新闻标题 / 摘要</TableHead>
                <TableHead className="w-[110px]">来源 / 时间</TableHead>
                <TableHead className="w-[90px]">影响程度</TableHead>
                <TableHead className="w-[48px]" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {news.map((item) => (
                <TableRow key={item.id} className="group">
                  <TableCell className="py-3">
                    {getSentimentBadge(item.sentiment?.label)}
                  </TableCell>
                  <TableCell className="py-3">
                    <div className="space-y-0.5">
                      <a
                        href={item.url === "#" ? undefined : item.url}
                        target="_blank"
                        rel="noreferrer"
                        className="block text-xs font-semibold text-[#e0e0e0] group-hover:text-[#2962ff] transition-colors cursor-pointer leading-snug"
                      >
                        {item.title}
                      </a>
                      {item.description && (
                        <div className="text-[10px] text-[#8b949e] line-clamp-1 leading-snug">
                          {item.description}
                        </div>
                      )}
                    </div>
                  </TableCell>
                  <TableCell className="py-3">
                    <div className="text-[11px] font-medium text-[#e0e0e0]">{item.source || "Yahoo Finance"}</div>
                    <div className="text-[10px] text-[#8b949e]">{item.published}</div>
                  </TableCell>
                  <TableCell className="py-3">
                    {getImpactBadge(item.impact)}
                  </TableCell>
                  <TableCell className="py-3">
                    <a
                      href={item.url === "#" ? undefined : item.url}
                      target="_blank"
                      rel="noreferrer"
                      className="p-1.5 rounded-md inline-flex hover:bg-[#2a2e39] text-[#8b949e] hover:text-white transition-colors"
                    >
                      <ExternalLink size={13} />
                    </a>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </ScrollArea>
      </div>
    </div>
  );
}

// ── 主组件: SentimentPanel ─────────────────────────────────────────────────

export default function SentimentPanel() {
  const [symbol, setSymbol] = useState("AAPL");
  const [maxItems, setMaxItems] = useState(10);
  const [loading, setLoading] = useState(false);
  const [aggregate, setAggregate] = useState<AggregateData>(EMPTY_AGGREGATE);
  const [history, setHistory] = useState<SentimentHistoryPoint[]>([]);
  const [news, setNews] = useState<NewsSentimentItem[]>([]);

  const fetchSentiment = useCallback(async () => {
    setLoading(true);
    try {
      const [newsRes, histRes] = await Promise.all([
        fetch(`/api/sentiment/news?symbol=${encodeURIComponent(symbol)}&max_items=${maxItems}`),
        fetch(`/api/sentiment/history?symbol=${encodeURIComponent(symbol)}`),
      ]);

      if (newsRes.ok) {
        const data = (await newsRes.json()) as {
          aggregate: AggregateData;
          items: NewsSentimentItem[];
        };
        setAggregate(data.aggregate);
        setNews(data.items);
      }

      if (histRes.ok) {
        const data = (await histRes.json()) as { history: SentimentHistoryPoint[] };
        setHistory(data.history);
      }
    } finally {
      setLoading(false);
    }
  }, [symbol, maxItems]);

  // 挂载时自动拉取一次（使用默认 symbol=AAPL）
  useEffect(() => {
    void fetchSentiment();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <TooltipProvider>
      <div
        className="flex overflow-hidden bg-[#131722]"
        style={{ height: "calc(100vh - 130px)" }}
      >
        {/* ── 侧栏 ───────────────────────────────────────────────────── */}
        <aside className="w-64 shrink-0 border-r border-[#2a2e39] bg-[#1a1e2e] flex flex-col p-4 gap-5 overflow-y-auto">
          {/* Header */}
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-white flex items-center gap-2">
              <Settings2 size={15} className="text-[#2962ff]" />
              情绪分析配置
            </h2>
            <Tooltip>
              <TooltipTrigger asChild>
                <button className="p-1 rounded hover:bg-[#2a2e39] text-[#8b949e] hover:text-white transition-colors">
                  <Info size={13} />
                </button>
              </TooltipTrigger>
              <TooltipContent side="bottom">
                <p>配置分析标的代码和样本数量</p>
              </TooltipContent>
            </Tooltip>
          </div>

          {/* Symbol input */}
          <div className="space-y-2">
            <Label htmlFor="sent-symbol" className="text-[10px] text-[#8b949e] uppercase tracking-wider font-bold">
              标的代码
            </Label>
            <div className="relative">
              <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-[#8b949e]" />
              <input
                id="sent-symbol"
                value={symbol}
                onChange={(e) => setSymbol(e.target.value.toUpperCase())}
                className="w-full pl-8 pr-3 py-2 text-xs bg-[#2a2e39] border border-[#363a45] rounded-lg text-white placeholder-[#8b949e] focus:outline-none focus:border-[#2962ff] transition-colors"
                placeholder="例如: AAPL"
                onKeyDown={(e) => { if (e.key === "Enter") void fetchSentiment(); }}
              />
            </div>
          </div>

          {/* Slider */}
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <Label className="text-[10px] text-[#8b949e] uppercase tracking-wider font-bold">
                分析条数
              </Label>
              <span className="text-xs font-mono bg-[#2962ff]/10 text-[#2962ff] px-1.5 py-0.5 rounded">
                {maxItems}
              </span>
            </div>
            <Slider
              value={[maxItems]}
              onValueChange={(val) => setMaxItems(val[0])}
              min={5}
              max={100}
              step={5}
            />
            <div className="flex justify-between text-[9px] text-[#434651] px-0.5">
              <span>5</span>
              <span>50</span>
              <span>100</span>
            </div>
          </div>

          {/* Aggregate stats */}
          <div className="border-t border-[#2a2e39] pt-4 space-y-2">
            <div className="text-[10px] text-[#8b949e] uppercase tracking-wider font-bold">汇总数据</div>
            {[
              ["新闻总数", String(aggregate.count)],
              ["平均复合分", aggregate.avg_compound.toFixed(4)],
              ["正面比例", `${(aggregate.positive_ratio * 100).toFixed(1)}%`],
              ["情绪指数", String(aggregate.score)],
            ].map(([k, v]) => (
              <div key={k} className="flex justify-between text-xs">
                <span className="text-[#8b949e]">{k}</span>
                <span className="text-white font-mono">{v}</span>
              </div>
            ))}
          </div>

          {/* Fetch button */}
          <div className="mt-auto space-y-2">
            <button
              onClick={() => void fetchSentiment()}
              disabled={loading}
              className="w-full flex items-center justify-center gap-2 py-2.5 px-4 bg-[#2962ff] hover:bg-[#2962ff]/90 disabled:opacity-60 text-white text-xs font-bold rounded-lg shadow-lg shadow-[#2962ff]/20 transition-all"
            >
              {loading ? (
                <>
                  <RefreshCw size={13} className="animate-spin" />
                  获取中…
                </>
              ) : (
                <>
                  <RefreshCw size={13} />
                  获取情绪分析
                </>
              )}
            </button>
            <p className="text-[10px] text-center text-[#434651] italic">
              数据由 QuantBot AI 实时处理
            </p>
          </div>
        </aside>

        {/* ── 主内容区 ──────────────────────────────────────────────────── */}
        <main className="flex-1 min-w-0 overflow-y-auto p-4 flex flex-col gap-4">
          {/* 顶部: 情绪仪表 + 趋势图 */}
          <div className="grid grid-cols-3 gap-4" style={{ minHeight: 260 }}>
            <div className="col-span-1">
              <SentimentGauge score={aggregate.score} />
            </div>
            <div className="col-span-2">
              <SentimentChart data={history} />
            </div>
          </div>

          {/* 底部: 新闻列表 */}
          <div className="flex-1 min-h-0" style={{ minHeight: 260 }}>
            <NewsFeed news={news} total={aggregate.count} />
          </div>
        </main>
      </div>
    </TooltipProvider>
  );
}

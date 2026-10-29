/**
 * 关注列表 — 实时价格 + 点击切换标的.
 * 支持两个 tab：默认关注列表 + 用户自选股（localStorage 持久化）.
 */
import { useEffect, useState, useRef } from "react";
import { Plus, X } from "lucide-react";
import FeatureGuideButton from "@/components/guides/FeatureGuideButton";
import { useChartStore } from "../../store/chartStore";
import { cn } from "../../lib/utils";

const DEFAULT_SYMBOLS = ["AAPL", "TSLA", "NVDA", "MSFT", "AMZN", "GOOGL", "META", "NFLX"];
const STORAGE_KEY = "quantpilot_custom_watchlist";

function loadCustomList(): string[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return JSON.parse(raw) as string[];
  } catch { /* ignore */ }
  return [];
}

function saveCustomList(list: string[]) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(list));
  } catch { /* ignore */ }
}

export default function ChartWatchlist() {
  const { symbol: activeSymbol, setSymbol } = useChartStore();
  const [prices, setPrices] = useState<Record<string, number>>({});
  const [tab, setTab] = useState<"default" | "custom">("default");
  const [customList, setCustomList] = useState<string[]>(loadCustomList);
  const [addInput, setAddInput] = useState("");
  const [showAdd, setShowAdd] = useState(false);
  const addInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const fetchPrices = async () => {
      try {
        const res = await fetch("/api/data/prices");
        if (res.ok) {
          const data = (await res.json()) as Record<string, string>;
          const parsed: Record<string, number> = {};
          for (const [sym, p] of Object.entries(data)) {
            parsed[sym] = parseFloat(p);
          }
          setPrices(parsed);
        }
      } catch { /* price cache may be empty */ }
    };
    void fetchPrices();
    const id = setInterval(() => void fetchPrices(), 5000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    if (showAdd) setTimeout(() => addInputRef.current?.focus(), 50);
  }, [showAdd]);

  const addCustomSymbol = () => {
    const sym = addInput.trim().toUpperCase();
    if (!sym || customList.includes(sym)) {
      setAddInput("");
      setShowAdd(false);
      return;
    }
    const next = [...customList, sym];
    setCustomList(next);
    saveCustomList(next);
    setAddInput("");
    setShowAdd(false);
  };

  const removeCustomSymbol = (sym: string) => {
    const next = customList.filter((s) => s !== sym);
    setCustomList(next);
    saveCustomList(next);
  };

  const displayList = tab === "default"
    ? [...DEFAULT_SYMBOLS, ...Object.keys(prices).filter((s) => !DEFAULT_SYMBOLS.includes(s))]
    : customList;
  const guideKey = tab === "default"
    ? "market.chart.watchlist.default"
    : "market.chart.watchlist.custom";

  return (
    <div className="relative flex flex-col h-1/2 border-b border-gray-800">
      {/* Tab 切换 */}
      <div className="flex border-b border-gray-800 text-[10px] uppercase font-bold text-gray-500 shrink-0">
        <button
          className={cn(
            "flex-1 py-2 transition-colors",
            tab === "default"
              ? "border-b-2 border-blue-500 text-blue-500"
              : "hover:text-white",
          )}
          onClick={() => setTab("default")}
        >
          关注列表
        </button>
        <button
          className={cn(
            "flex-1 py-2 transition-colors",
            tab === "custom"
              ? "border-b-2 border-blue-500 text-blue-500"
              : "hover:text-white",
          )}
          onClick={() => setTab("custom")}
        >
          自选股
          {customList.length > 0 && (
            <span className="ml-1 text-[9px] bg-blue-600/30 text-blue-400 rounded-full px-1">
              {customList.length}
            </span>
          )}
        </button>
      </div>

      {/* 列表 */}
      <div className="flex-1 overflow-y-auto custom-scrollbar">
        {displayList.length === 0 && tab === "custom" ? (
          <div className="flex flex-col items-center justify-center h-full gap-2 text-gray-600 text-[11px] px-4 text-center">
            <p>自选股为空</p>
            <p>点击下方 + 按钮添加标的</p>
          </div>
        ) : (
          displayList.map((sym) => {
            const price = prices[sym];
            const isActive = sym === activeSymbol;
            return (
              <div
                key={sym}
                className={cn(
                  "flex items-center justify-between px-3 py-2.5 cursor-pointer border-b border-gray-800/50 transition-colors group",
                  isActive ? "bg-[#1e3a5f]" : "hover:bg-[#2a2e39]",
                )}
                onClick={() => setSymbol(sym)}
              >
                <div className="flex flex-col">
                  <span className="text-sm font-bold text-white">{sym}</span>
                  <span className="text-[10px] text-gray-500 font-mono">
                    {price != null ? `$${price.toFixed(2)}` : "—"}
                  </span>
                </div>
                <div className="flex items-center gap-1">
                  {isActive && (
                    <span className="text-[10px] text-blue-400 font-bold">当前</span>
                  )}
                  {tab === "custom" && (
                    <button
                      onClick={(e) => { e.stopPropagation(); removeCustomSymbol(sym); }}
                      className="opacity-0 group-hover:opacity-100 text-gray-600 hover:text-red-400 transition-all p-0.5 rounded"
                      title="移除"
                    >
                      <X size={12} />
                    </button>
                  )}
                </div>
              </div>
            );
          })
        )}
      </div>

      {/* 自选股添加栏 */}
      {tab === "custom" && (
        <div className="shrink-0 border-t border-gray-800 p-2">
          {showAdd ? (
            <div className="flex gap-1">
              <input
                ref={addInputRef}
                value={addInput}
                onChange={(e) => setAddInput(e.target.value.toUpperCase())}
                onKeyDown={(e) => {
                  if (e.key === "Enter") addCustomSymbol();
                  if (e.key === "Escape") { setShowAdd(false); setAddInput(""); }
                }}
                placeholder="输入标的代码…"
                className="flex-1 bg-[#2a2e39] border border-gray-700 rounded px-2 py-1 text-xs text-white outline-none focus:border-blue-500 placeholder-gray-600 font-mono"
              />
              <button
                onClick={addCustomSymbol}
                className="px-2 py-1 bg-blue-600 hover:bg-blue-500 text-white text-[10px] rounded transition-colors font-bold"
              >
                添加
              </button>
            </div>
          ) : (
            <button
              onClick={() => setShowAdd(true)}
              className="w-full flex items-center justify-center gap-1 text-[10px] text-gray-500 hover:text-white hover:bg-[#2a2e39] rounded py-1.5 transition-colors"
            >
              <Plus size={12} />
              添加自选股
            </button>
          )}
        </div>
      )}
      <FeatureGuideButton guideKey={guideKey} className="bottom-3 right-3" />
    </div>
  );
}

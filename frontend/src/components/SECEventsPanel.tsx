/**
 * SEC Events Panel — EDGAR 8-K diff + Form 4 内部人集群信号（phaseF2.sec-events-panel）
 *
 * 展示：
 *  - 最新 8-K item-level 变化（新增/删除/修改 段落着色）
 *  - Form 4 内部人集群买入信号（signal_strength 进度条 + 明细展开）
 *  - 双重信号解读提示
 *
 * 数据来源：GET /api/sec/summary?ticker=...
 */
import { useCallback, useState } from "react";
import { ChevronDown, ChevronUp, FileText, RefreshCw, Users } from "lucide-react";

import { cn } from "../lib/utils";
import {
  fetchSECSummary,
  type SECInsiderCluster,
  type SECSummary,
} from "../api/client";

// ── helpers ───────────────────────────────────────────────────────────────────

function fmtDollar(n: number): string {
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs >= 1e9) return `${sign}$${(abs / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${sign}$${(abs / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${sign}$${(abs / 1e3).toFixed(1)}K`;
  return `${sign}$${abs.toFixed(0)}`;
}

function fmtDate(s: string): string {
  try {
    return new Date(s).toLocaleDateString("en-US", {
      year: "numeric", month: "short", day: "numeric",
    });
  } catch {
    return s;
  }
}

// ── diff type styling ─────────────────────────────────────────────────────────

type DiffType = "added" | "removed" | "modified" | "unchanged";

function diffClass(t: DiffType): string {
  if (t === "added")    return "bg-emerald-900/30 border-emerald-700/40 text-emerald-400";
  if (t === "removed")  return "bg-red-900/30 border-red-700/40 text-red-400";
  if (t === "modified") return "bg-yellow-900/30 border-yellow-700/40 text-yellow-400";
  return "text-[#8E9299]";
}

function diffLabel(t: DiffType): string {
  if (t === "added")    return "＋ New";
  if (t === "removed")  return "－ Removed";
  if (t === "modified") return "✎ Changed";
  return "≡";
}

// ── sub-components ────────────────────────────────────────────────────────────

function SignalBar({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const color =
    value >= 0.7 ? "bg-[#00C087]" :
    value >= 0.4 ? "bg-yellow-400" :
    "bg-[#8E9299]";
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 bg-[#0E1014] rounded-full h-2 overflow-hidden">
        <div className={cn("h-2 rounded-full transition-all", color)} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs font-mono text-[#8E9299] w-8 text-right">{pct}%</span>
    </div>
  );
}

function ClusterCard({ cluster }: { cluster: SECInsiderCluster }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="bg-[#0E1014] border border-[#2A2D35] rounded-lg p-3 space-y-2">
      <div className="flex items-start justify-between gap-2">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-white text-sm font-bold">
              {cluster.insider_count} insiders
            </span>
            <span className="text-[#8E9299] text-xs">
              {cluster.key_roles.join(" + ")}
            </span>
          </div>
          <div className="text-[#8E9299] text-xs mt-0.5">
            {fmtDate(cluster.window_start)} – {fmtDate(cluster.window_end)}
            &nbsp;·&nbsp;
            <span className="text-[#00C087] font-bold">{fmtDollar(cluster.total_value)}</span> total
          </div>
        </div>
        <button
          onClick={() => setExpanded(!expanded)}
          className="text-[#8E9299] hover:text-white transition-colors shrink-0"
          aria-label="Toggle details"
        >
          {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
        </button>
      </div>

      <SignalBar value={cluster.signal_strength} />

      {expanded && cluster.transactions.length > 0 && (
        <div className="pt-1 space-y-1 border-t border-[#2A2D35]">
          {cluster.transactions.map((t, i) => (
            <div key={i} className="flex justify-between items-start text-xs gap-2">
              <div>
                <span className="text-white">{t.insider_name}</span>
                <span className="text-[#8E9299] ml-1">({t.insider_title})</span>
              </div>
              <div className="text-right shrink-0">
                <span className="text-[#00C087]">{fmtDollar(t.total_value)}</span>
                <span className="text-[#8E9299] ml-1">@ ${t.price_per_share.toFixed(2)}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function EightKDiffSection({ summary }: { summary: SECSummary }) {
  const diff = summary["8k_diff"];
  const latestItems = summary.latest_8k.items ?? [];

  if (!diff.has_diff) {
    return (
      <div className="text-[#8E9299] text-sm">
        Fewer than 2 8-K filings available for diff.
      </div>
    );
  }

  const changeScore = diff.overall_change_score ?? 0;
  const materialChange = diff.has_material_change ?? false;
  const changedItems = diff.changed_items ?? [];

  return (
    <div className="space-y-3">
      {/* Score bar */}
      <div className="flex items-center gap-3">
        <div className="flex-1">
          <SignalBar value={changeScore} />
        </div>
        <span className={cn(
          "text-xs font-bold px-2 py-0.5 rounded",
          materialChange
            ? "bg-yellow-900/40 text-yellow-400 border border-yellow-700/30"
            : "bg-[#0E1014] text-[#8E9299] border border-[#2A2D35]",
        )}>
          {materialChange ? "Material Change" : "No Material Change"}
        </span>
      </div>

      {/* Changed items */}
      {changedItems.length > 0 && (
        <div className="flex flex-wrap gap-1">
          <span className="text-[#8E9299] text-xs">Changed items:</span>
          {changedItems.map((num) => (
            <span
              key={num}
              className="text-xs bg-yellow-900/30 text-yellow-400 border border-yellow-700/30 px-1.5 py-0.5 rounded"
            >
              Item {num}
            </span>
          ))}
        </div>
      )}

      {/* Latest 8-K items snippets */}
      {latestItems.length > 0 && (
        <div className="space-y-2">
          {latestItems.map((item) => {
            const changed = changedItems.includes(item.item_number);
            const diffType = changed ? "modified" as const : "unchanged" as const;
            return (
              <div
                key={item.item_number}
                className={cn(
                  "border rounded-lg p-2",
                  changed
                    ? "bg-yellow-900/20 border-yellow-700/30"
                    : "bg-[#0E1014] border-[#2A2D35]",
                )}
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="text-xs font-bold text-white">
                    Item {item.item_number} — {item.item_title}
                  </div>
                  {changed && (
                    <span className={cn("text-[10px] px-1.5 py-0.5 rounded border", diffClass(diffType))}>
                      {diffLabel(diffType)}
                    </span>
                  )}
                </div>
                <div className="text-[#8E9299] text-xs mt-0.5 line-clamp-2">
                  {item.text_snippet}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── main component ────────────────────────────────────────────────────────────

export default function SECEventsPanel() {
  const [inputTicker, setInputTicker] = useState("AAPL");
  const [summary, setSummary] = useState<SECSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const t = inputTicker.trim().toUpperCase();
    if (!t) return;
    setLoading(true);
    setError(null);
    try {
      const data = await fetchSECSummary(t);
      setSummary(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [inputTicker]);

  const hasClusters = (summary?.insider_clusters.length ?? 0) > 0;
  const hasMaterialChange = summary?.["8k_diff"]?.has_material_change ?? false;

  return (
    <section className="bg-[#151619] border border-[#2A2D35] rounded-xl p-5 space-y-4">
      {/* ── Header ── */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-2">
          <FileText size={18} className="text-[#00C087]" />
          <h2 className="text-white font-bold text-lg">SEC 事件信号</h2>
          <span className="text-[#8E9299] text-xs">8-K Diff · Form 4 Cluster</span>
        </div>

        <div className="flex items-center gap-2">
          <input
            value={inputTicker}
            onChange={(e) => setInputTicker(e.target.value.toUpperCase())}
            onKeyDown={(e) => e.key === "Enter" && void refresh()}
            placeholder="AAPL"
            className="w-24 bg-[#0E1014] border border-[#2A2D35] text-white text-xs rounded px-2 py-1 font-mono placeholder:text-[#4A4D55] focus:outline-none focus:border-[#00C087]/60"
          />
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
            {loading ? "加载中…" : "查询"}
          </button>
        </div>
      </div>

      {/* ── Error ── */}
      {error && (
        <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-3 text-red-400 text-sm">
          ⚠ {error}
        </div>
      )}

      {summary && (
        <>
          {/* ── Signal summary badges ── */}
          <div className="flex flex-wrap gap-2">
            <div className="bg-[#0E1014] border border-[#2A2D35] rounded-lg px-3 py-1.5">
              <span className="text-[#8E9299] text-[10px] font-bold uppercase tracking-wider">Ticker</span>
              <div className="text-white font-bold font-mono">{summary.ticker}</div>
            </div>
            {summary.latest_8k.filed_date && (
              <div className="bg-[#0E1014] border border-[#2A2D35] rounded-lg px-3 py-1.5">
                <span className="text-[#8E9299] text-[10px] font-bold uppercase tracking-wider">Latest 8-K</span>
                <div className="text-white text-sm">{fmtDate(summary.latest_8k.filed_date)}</div>
              </div>
            )}
            <div className={cn(
              "border rounded-lg px-3 py-1.5",
              hasMaterialChange ? "bg-yellow-900/20 border-yellow-700/30" : "bg-[#0E1014] border-[#2A2D35]",
            )}>
              <span className="text-[#8E9299] text-[10px] font-bold uppercase tracking-wider">8-K Change</span>
              <div className={hasMaterialChange ? "text-yellow-400 text-sm font-bold" : "text-[#8E9299] text-sm"}>
                {hasMaterialChange ? "Material" : "No change"}
              </div>
            </div>
            <div className={cn(
              "border rounded-lg px-3 py-1.5",
              hasClusters ? "bg-emerald-900/20 border-emerald-700/30" : "bg-[#0E1014] border-[#2A2D35]",
            )}>
              <span className="text-[#8E9299] text-[10px] font-bold uppercase tracking-wider">Insider Signal</span>
              <div className={hasClusters ? "text-[#00C087] text-sm font-bold" : "text-[#8E9299] text-sm"}>
                {hasClusters ? `${summary.insider_clusters.length} cluster(s)` : "No cluster"}
              </div>
            </div>
          </div>

          {/* ── 8-K diff section ── */}
          <div>
            <div className="text-[#8E9299] text-[11px] font-bold uppercase tracking-wider mb-2">
              8-K 最新变化（差分）
            </div>
            <EightKDiffSection summary={summary} />
          </div>

          {/* ── Form 4 clusters ── */}
          {hasClusters && (
            <div>
              <div className="flex items-center gap-2 mb-2">
                <Users size={12} className="text-[#00C087]" />
                <span className="text-[#8E9299] text-[11px] font-bold uppercase tracking-wider">
                  内部人集群买入（90 日窗口）
                </span>
              </div>
              <div className="space-y-2">
                {summary.insider_clusters.map((c, i) => (
                  <ClusterCard key={i} cluster={c} />
                ))}
              </div>
            </div>
          )}

          {/* ── Interpretation ── */}
          <div className="bg-[#0E1014] border border-[#2A2D35] rounded-lg p-3 space-y-1.5 text-xs">
            <div className="text-[#8E9299] text-[10px] font-bold uppercase tracking-wider">
              交易解读
            </div>
            {hasMaterialChange && hasClusters ? (
              <div className="text-[#00C087]">
                ⚡ 双重看涨信号：8-K 存在实质性变化 + 内部人集群买入 —— 高置信度看多信号
              </div>
            ) : hasClusters ? (
              <div className="text-[#8E9299]">
                <span className="text-[#00C087]">内部人集群买入</span>{" "}
                → 90 日内多名高管买入，历史超额回报 +6~10%（180 日）
              </div>
            ) : hasMaterialChange ? (
              <div className="text-[#8E9299]">
                <span className="text-yellow-400">8-K 实质性变化</span>{" "}
                → 建议精读原文，关注 Item 5.02（高管变动）和 Item 8.01（其他重大事项）
              </div>
            ) : (
              <div className="text-[#8E9299]">暂无事件信号，当前持仓维持正常观察状态</div>
            )}
          </div>
        </>
      )}

      {/* ── Empty state ── */}
      {!summary && !loading && !error && (
        <div className="text-center py-10 text-[#8E9299] text-sm">
          输入股票代码后点击「查询」加载 SEC 事件信号
        </div>
      )}
    </section>
  );
}

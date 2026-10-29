/**
 * 交易信号广播面板.
 */
import { useCallback, useEffect, useState } from "react";
import { cn } from "../lib/utils";

interface Signal {
  id: number;
  source: string;
  symbol: string;
  action: string;
  price: number;
  confidence: number;
  reason: string;
  timestamp: string;
}

export default function SignalsPanel() {
  const [signals, setSignals] = useState<Signal[]>([]);
  const [filterSymbol, setFilterSymbol] = useState("");
  const [form, setForm] = useState({
    source: "manual",
    symbol: "AAPL",
    action: "buy",
    price: "150",
    confidence: "0.8",
    reason: "",
  });
  const [message, setMessage] = useState<string | null>(null);

  const loadFeed = useCallback(async () => {
    try {
      const qs = filterSymbol ? `?symbol=${filterSymbol}` : "";
      const res = await fetch(`/api/signals/feed${qs}`);
      if (res.ok) {
        const d = (await res.json()) as { signals: Signal[]; count: number };
        setSignals(d.signals);
      }
    } catch (_) {}
  }, [filterSymbol]);

  useEffect(() => {
    void loadFeed();
  }, [loadFeed]);

  const publishSignal = async () => {
    try {
      const res = await fetch("/api/signals/publish", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source: form.source,
          symbol: form.symbol,
          action: form.action,
          price: parseFloat(form.price),
          confidence: parseFloat(form.confidence),
          reason: form.reason,
        }),
      });
      if (res.ok) {
        const d = (await res.json()) as { message: string };
        setMessage(d.message);
        void loadFeed();
      }
    } catch (e) {
      setMessage(String(e));
    }
  };

  const actionColor = (a: string) =>
    a === "buy"
      ? "text-[#00C087]"
      : a === "sell"
        ? "text-[#FF4D4D]"
        : "text-[#8E9299]";

  return (
    <div className="flex gap-4">
      {/* ── 左侧：发布信号 ────────────────────────── */}
      <div className="w-72 shrink-0 bg-[#151619] border border-[#2A2D35] rounded-xl p-5 flex flex-col gap-3">
        <p className="text-xs font-mono uppercase tracking-wider text-[#8E9299]">
          发布信号
        </p>

        {(
          [
            ["source", "来源"],
            ["symbol", "标的代码"],
            ["price", "价格"],
            ["confidence", "置信度 (0-1)"],
            ["reason", "原因"],
          ] as [keyof typeof form, string][]
        ).map(([k, label]) => (
          <div key={k} className="flex flex-col gap-1">
            <label className="text-[10px] text-[#8E9299]">{label}</label>
            <input
              value={form[k]}
              onChange={(e) => setForm((p) => ({ ...p, [k]: e.target.value }))}
              className="w-full bg-[#1C1E22] border border-[#2A2D35] rounded-lg px-3 py-1.5 text-xs text-white placeholder-[#4A4D55] outline-none focus:border-[#4A4D55] transition-colors"
            />
          </div>
        ))}

        <div className="flex flex-col gap-1">
          <label className="text-[10px] text-[#8E9299]">动作</label>
          <select
            value={form.action}
            onChange={(e) => setForm((p) => ({ ...p, action: e.target.value }))}
            className="w-full bg-[#1C1E22] border border-[#2A2D35] rounded-lg px-3 py-1.5 text-xs text-white outline-none focus:border-[#4A4D55] transition-colors"
          >
            <option value="buy">买入 (buy)</option>
            <option value="sell">卖出 (sell)</option>
            <option value="hold">持有 (hold)</option>
          </select>
        </div>

        <button
          onClick={() => void publishSignal()}
          className="py-2 bg-blue-600 hover:bg-blue-500 text-white rounded-lg text-xs font-semibold transition-colors"
        >
          发布
        </button>

        {message && (
          <p className="text-xs text-[#00C087]">{message}</p>
        )}
      </div>

      {/* ── 右侧：信号列表 ────────────────────────── */}
      <div className="flex-1 bg-[#151619] border border-[#2A2D35] rounded-xl p-6">
        <div className="flex items-center gap-3 mb-5">
          <p className="text-xs font-mono uppercase tracking-wider text-[#8E9299] flex-1">
            信号列表
            <span className="ml-2 normal-case text-[#4A4D55]">({signals.length})</span>
          </p>
          <input
            value={filterSymbol}
            onChange={(e) => setFilterSymbol(e.target.value)}
            placeholder="过滤标的"
            className="w-28 bg-[#1C1E22] border border-[#2A2D35] rounded-lg px-3 py-1.5 text-xs text-white placeholder-[#4A4D55] outline-none focus:border-[#4A4D55] transition-colors"
          />
        </div>

        {signals.length === 0 ? (
          <div className="flex items-center justify-center h-32 text-[#4A4D55] text-sm">
            暂无信号
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse">
              <thead>
                <tr className="border-b border-[#2A2D35]">
                  {["#", "来源", "标的", "动作", "价格", "置信度", "原因", "时间"].map((h) => (
                    <th
                      key={h}
                      className="px-3 py-2 text-[10px] font-mono uppercase tracking-wider text-[#8E9299] whitespace-nowrap"
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-[#2A2D35]">
                {signals.map((s) => (
                  <tr
                    key={s.id}
                    className="hover:bg-[#1C1E22]/30 transition-colors"
                  >
                    <td className="px-3 py-3 text-xs text-[#4A4D55] font-mono">{s.id}</td>
                    <td className="px-3 py-3 text-xs text-[#8E9299]">{s.source}</td>
                    <td className="px-3 py-3 text-sm font-bold text-white">{s.symbol}</td>
                    <td className={cn("px-3 py-3 text-xs font-bold uppercase", actionColor(s.action))}>
                      {s.action}
                    </td>
                    <td className="px-3 py-3 text-xs font-mono text-[#E1E4E8]">${s.price}</td>
                    <td className="px-3 py-3 text-xs font-mono text-[#8E9299]">
                      {(s.confidence * 100).toFixed(0)}%
                    </td>
                    <td className="px-3 py-3 text-xs text-[#8E9299] max-w-32 truncate">
                      {s.reason}
                    </td>
                    <td className="px-3 py-3 text-[10px] text-[#4A4D55] font-mono whitespace-nowrap">
                      {s.timestamp.slice(0, 19)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

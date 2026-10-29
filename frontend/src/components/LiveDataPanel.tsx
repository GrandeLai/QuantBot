/**
 * 实时行情 & 信号面板 — WebSocket 订阅.
 */
import { useEffect, useRef, useState } from "react";

interface BarMsg {
  symbol: string;
  timeframe: string;
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

interface SignalMsg {
  id: number;
  source: string;
  symbol: string;
  action: string;
  price: number;
  confidence: number;
  reason: string;
  timestamp: string;
}

function useWebSocket<T>(
  url: string,
  enabled: boolean,
): { messages: T[]; status: string } {
  const [messages, setMessages] = useState<T[]>([]);
  const [status, setStatus] = useState("disconnected");
  const ws = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!enabled) return;
    setStatus("connecting");
    const socket = new WebSocket(url);
    ws.current = socket;

    socket.onopen = () => setStatus("connected");
    socket.onclose = () => setStatus("disconnected");
    socket.onerror = () => setStatus("error");
    socket.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data as string) as T;
        setMessages((prev) => [msg, ...prev].slice(0, 50));
      } catch (_) {}
    };
    return () => {
      socket.close();
    };
  }, [url, enabled]);

  return { messages, status };
}

const statusColor = (s: string) =>
  s === "connected" ? "#34d399" : s === "connecting" ? "#fbbf24" : "#f87171";

export default function LiveDataPanel() {
  const [symbol, setSymbol] = useState("AAPL");
  const [timeframe, setTimeframe] = useState("1d");
  const [barEnabled, setBarEnabled] = useState(false);
  const [signalEnabled, setSignalEnabled] = useState(false);
  const [wsBase, setWsBase] = useState("ws://localhost:8001");

  const barUrl = `${wsBase}/ws/bars/${symbol}/${timeframe}`;
  const signalUrl = `${wsBase}/ws/signals`;

  const { messages: bars, status: barStatus } = useWebSocket<BarMsg>(barUrl, barEnabled);
  const { messages: signals, status: sigStatus } = useWebSocket<SignalMsg>(signalUrl, signalEnabled);

  const inputSt = {
    padding: "5px 8px", fontSize: 12, background: "#1e293b",
    border: "1px solid #334155", borderRadius: 4, color: "#e2e8f0", outline: "none",
  } as const;

  const actionColor = (a: string) =>
    a === "buy" ? "#34d399" : a === "sell" ? "#f87171" : "#64748b";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {/* 配置区 */}
      <div style={{ background: "#0f172a", border: "1px solid #1e293b", borderRadius: 8, padding: 16, display: "flex", gap: 16, alignItems: "flex-end", flexWrap: "wrap" }}>
        <div>
          <div style={{ fontSize: 11, color: "#64748b", marginBottom: 4 }}>WebSocket 服务地址</div>
          <input value={wsBase} onChange={(e) => setWsBase(e.target.value)}
            style={{ ...inputSt, width: 220 }} />
        </div>
        <div>
          <div style={{ fontSize: 11, color: "#64748b", marginBottom: 4 }}>标的</div>
          <input value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())}
            style={{ ...inputSt, width: 80 }} />
        </div>
        <div>
          <div style={{ fontSize: 11, color: "#64748b", marginBottom: 4 }}>周期</div>
          <select value={timeframe} onChange={(e) => setTimeframe(e.target.value)}
            style={{ ...inputSt, background: "#1e293b" }}>
            {["1m","5m","15m","1h","4h","1d"].map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button
            onClick={() => setBarEnabled((v) => !v)}
            style={{ padding: "6px 14px", fontSize: 12, fontWeight: 600, border: "none", borderRadius: 4, cursor: "pointer", background: barEnabled ? "#1e3a5f" : "#1e293b", color: barEnabled ? "#60a5fa" : "#64748b" }}>
            {barEnabled ? "停止行情" : "订阅行情"}
          </button>
          <button
            onClick={() => setSignalEnabled((v) => !v)}
            style={{ padding: "6px 14px", fontSize: 12, fontWeight: 600, border: "none", borderRadius: 4, cursor: "pointer", background: signalEnabled ? "#1e3a5f" : "#1e293b", color: signalEnabled ? "#60a5fa" : "#64748b" }}>
            {signalEnabled ? "停止信号" : "订阅信号"}
          </button>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
        {/* Bar stream */}
        <div style={{ background: "#0f172a", border: "1px solid #1e293b", borderRadius: 8, padding: 16 }}>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 12 }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: "#94a3b8" }}>
              实时行情 — {symbol}/{timeframe}
            </div>
            <div style={{ fontSize: 11, color: statusColor(barStatus), fontWeight: 600 }}>
              ● {barStatus}
            </div>
          </div>
          {bars.length === 0 ? (
            <div style={{ color: "#334155", fontSize: 12 }}>等待行情推送…</div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 6, maxHeight: 400, overflowY: "auto" }}>
              {bars.map((bar, i) => (
                <div key={i} style={{ background: "#1e293b", borderRadius: 4, padding: "8px 12px", fontSize: 12 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                    <span style={{ color: "#e2e8f0", fontWeight: 600 }}>{bar.symbol}</span>
                    <span style={{ color: "#64748b" }}>{bar.timestamp.slice(0, 19)}</span>
                  </div>
                  <div style={{ display: "flex", gap: 12, color: "#94a3b8" }}>
                    {[["O", bar.open], ["H", bar.high], ["L", bar.low], ["C", bar.close]].map(([k, v]) => (
                      <span key={k as string}><span style={{ color: "#475569" }}>{k}</span> {(v as number).toFixed(2)}</span>
                    ))}
                    <span style={{ color: "#475569" }}>Vol {(bar.volume / 1e6).toFixed(1)}M</span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Signal stream */}
        <div style={{ background: "#0f172a", border: "1px solid #1e293b", borderRadius: 8, padding: 16 }}>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 12 }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: "#94a3b8" }}>实时信号</div>
            <div style={{ fontSize: 11, color: statusColor(sigStatus), fontWeight: 600 }}>
              ● {sigStatus}
            </div>
          </div>
          {signals.length === 0 ? (
            <div style={{ color: "#334155", fontSize: 12 }}>等待信号推送…</div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 6, maxHeight: 400, overflowY: "auto" }}>
              {signals.map((sig, i) => (
                <div key={i} style={{ background: "#1e293b", borderRadius: 4, padding: "8px 12px", borderLeft: `3px solid ${actionColor(sig.action)}`, fontSize: 12 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 2 }}>
                    <span style={{ color: actionColor(sig.action), fontWeight: 700 }}>
                      {sig.action.toUpperCase()} {sig.symbol}
                    </span>
                    <span style={{ color: "#64748b" }}>{sig.confidence ? `${(sig.confidence * 100).toFixed(0)}%` : ""}</span>
                  </div>
                  <div style={{ color: "#64748b" }}>${sig.price} · {sig.source}</div>
                  {sig.reason && <div style={{ color: "#475569", marginTop: 2 }}>{sig.reason}</div>}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

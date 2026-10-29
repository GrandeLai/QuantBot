/**
 * 加密货币交易面板 — OKX 现货 / 合约 / 期权。
 *
 * 标签页：
 *   行情 — 现货 ticker 卡片网格
 *   交易 — 现货买卖下单
 *   持仓 — 账户余额
 *   订单 — 现货挂单
 *   合约 — 永续合约行情 / 交易 / 持仓 / 挂单
 *   期权 — 期权链（含希腊字母）/ 持仓
 */
import { useEffect, useState } from "react";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import CandlestickChart from "./CandlestickChart";
import type { OhlcBar } from "../api/client";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  cancelCryptoOrder,
  cancelFuturesOrder,
  getCryptoAccount,
  getCryptoOpenOrders,
  getCryptoPairs,
  getCryptoStatus,
  getCryptoTicker,
  getFuturesOpenOrders,
  getFuturesPositions,
  getFuturesTickers,
  getOptionsChain,
  getOptionsExpiries,
  getOptionsPositions,
  getOptionsUnderlyings,
  submitCryptoOrder,
  submitFuturesOrder,
  type CryptoBalance,
  type CryptoOrder,
  type CryptoPosition,
  type CryptoTicker,
  type OptionTicker,
  type SwapTicker,
} from "../api/crypto";
import { useCryptoStore, type CryptoActiveTab } from "../store/cryptoStore";
import {
  defaultCryptoBacktestStrategy,
  prioritizeCryptoStrategies,
  recommendedCryptoBacktestTimeframe,
} from "./cryptoBacktestConfig";

// ─── Helpers ────────────────────────────────────────────────────────────────

const fmt = (v: number) =>
  v >= 1000
    ? v.toLocaleString("en-US", { maximumFractionDigits: 2 })
    : v.toFixed(4);

const fmtPct = (v: number) => `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;

const fmtFunding = (r: number) =>
  `${(r * 100).toFixed(4)}%`;


// ─── Spot Sub-components ─────────────────────────────────────────────────────

function WatchlistItem({
  pair,
  selected,
  ticker,
  onClick,
}: {
  pair: { symbol: string; display: string };
  selected: boolean;
  ticker?: CryptoTicker;
  onClick: () => void;
}) {
  return (
    <div
      onClick={onClick}
      className={`cursor-pointer px-3 py-2.5 border-l-2 transition-colors ${
        selected
          ? "bg-[#2962ff]/10 border-l-[#2962ff] text-white"
          : "border-l-transparent text-[#8b949e] hover:text-white hover:bg-white/5"
      }`}
    >
      <div className="text-sm font-medium">{pair.display}</div>
      {ticker ? (
        <div className="flex items-center justify-between mt-0.5">
          <span className="text-xs font-mono text-[#8b949e]">${fmt(ticker.price)}</span>
          <span className={`text-[10px] font-medium ${ticker.change_pct >= 0 ? "text-green-400" : "text-red-400"}`}>
            {fmtPct(ticker.change_pct)}
          </span>
        </div>
      ) : null}
    </div>
  );
}

function MarketTab({ tickers }: { tickers: Record<string, CryptoTicker> }) {
  const tickerList = Object.values(tickers);
  if (tickerList.length === 0) {
    return <div className="text-center py-12 text-[#8b949e] text-sm">暂无行情数据，请点击刷新行情</div>;
  }
  return (
    <div className="grid grid-cols-2 gap-3 xl:grid-cols-3">
      {tickerList.map((t) => (
        <div key={t.symbol} className="bg-[#1e222d] border border-[#2a2e39] rounded-xl p-4 space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-sm font-semibold text-white">{t.display}</span>
            <span className={`text-xs font-medium px-1.5 py-0.5 rounded ${t.change_pct >= 0 ? "bg-green-400/10 text-green-400" : "bg-red-400/10 text-red-400"}`}>
              {fmtPct(t.change_pct)}
            </span>
          </div>
          <div className="text-xl font-bold font-mono text-white">${fmt(t.price)}</div>
          <div className="grid grid-cols-2 gap-1 text-xs text-[#8b949e]">
            <div><span className="text-[#434651]">24H高</span><div className="font-mono">{fmt(t.high_24h)}</div></div>
            <div><span className="text-[#434651]">24H低</span><div className="font-mono">{fmt(t.low_24h)}</div></div>
          </div>
          <div className="text-[10px] text-[#434651]">
            成交量 <span className="font-mono text-[#8b949e]">
              {t.volume_usdt >= 1_000_000 ? `$${(t.volume_usdt / 1_000_000).toFixed(2)}M` : `$${(t.volume_usdt / 1_000).toFixed(0)}K`}
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}

function TradeTab({ configured, selectedSymbol }: { configured: boolean; selectedSymbol: string }) {
  const { orderSide, orderType, orderQty, orderPrice, setOrderSide, setOrderType, setOrderQty, setOrderPrice, setOpenOrders } = useCryptoStore();
  const [submitting, setSubmitting] = useState(false);
  const [feedback, setFeedback] = useState<{ tone: "success" | "error"; message: string } | null>(null);

  if (!configured) {
    return (
      <div className="text-center py-8 space-y-3">
        <div className="text-[#8b949e] text-sm">需要配置 OKX API Key 才能交易</div>
        <div className="text-xs text-[#434651] font-mono">
          在 backend/.env 设置:<br />
          QUANTBOT_OKX_API_KEY=...<br />
          QUANTBOT_OKX_API_SECRET=...<br />
          QUANTBOT_OKX_PASSPHRASE=...<br />
          QUANTBOT_OKX_DEMO=true
        </div>
      </div>
    );
  }

  async function handleSubmit() {
    if (!orderQty || parseFloat(orderQty) <= 0) { setFeedback({ tone: "error", message: "请输入有效的交易数量" }); return; }
    if (orderType === "limit" && (!orderPrice || parseFloat(orderPrice) <= 0)) { setFeedback({ tone: "error", message: "限价单需要输入价格" }); return; }
    setSubmitting(true); setFeedback(null);
    try {
      await submitCryptoOrder({ symbol: selectedSymbol, side: orderSide, order_type: orderType, quantity: parseFloat(orderQty), ...(orderType === "limit" ? { price: parseFloat(orderPrice) } : {}) });
      setFeedback({ tone: "success", message: "下单成功！" });
      setOrderQty(""); setOrderPrice("");
      getCryptoOpenOrders().then(setOpenOrders).catch(() => {});
    } catch (e) {
      setFeedback({ tone: "error", message: String(e) });
    } finally { setSubmitting(false); }
  }

  return (
    <div className="max-w-sm space-y-4">
      <div className="text-sm text-[#8b949e]">交易对：<span className="text-white font-medium">{selectedSymbol}</span></div>
      <div className="flex rounded-lg overflow-hidden border border-[#2a2e39]">
        <button onClick={() => setOrderSide("buy")} className={`flex-1 py-2 text-sm font-medium transition-colors ${orderSide === "buy" ? "bg-green-500/20 text-green-400" : "text-[#8b949e] hover:text-white"}`}>买入</button>
        <button onClick={() => setOrderSide("sell")} className={`flex-1 py-2 text-sm font-medium transition-colors ${orderSide === "sell" ? "bg-red-500/20 text-red-400" : "text-[#8b949e] hover:text-white"}`}>卖出</button>
      </div>
      <div className="space-y-1">
        <label className="text-xs text-[#8b949e]">订单类型</label>
        <div className="flex gap-2">
          {(["market", "limit"] as const).map((t) => (
            <button key={t} onClick={() => setOrderType(t)} className={`px-3 py-1.5 rounded-lg text-xs font-medium border transition-colors ${orderType === t ? "bg-[#2962ff]/20 border-[#2962ff] text-[#2962ff]" : "border-[#2a2e39] text-[#8b949e] hover:text-white"}`}>
              {t === "market" ? "市价" : "限价"}
            </button>
          ))}
        </div>
      </div>
      <div className="space-y-1">
        <label className="text-xs text-[#8b949e]">数量</label>
        <input type="number" step="0.0001" min="0" placeholder="0.001" value={orderQty} onChange={(e) => setOrderQty(e.target.value)} className="w-full bg-[#1e222d] border border-[#2a2e39] rounded-lg px-3 py-2 text-white text-sm font-mono focus:outline-none focus:border-[#2962ff]" />
      </div>
      {orderType === "limit" && (
        <div className="space-y-1">
          <label className="text-xs text-[#8b949e]">限价</label>
          <input type="number" step="0.01" min="0" placeholder="0.00" value={orderPrice} onChange={(e) => setOrderPrice(e.target.value)} className="w-full bg-[#1e222d] border border-[#2a2e39] rounded-lg px-3 py-2 text-white text-sm font-mono focus:outline-none focus:border-[#2962ff]" />
        </div>
      )}
      {feedback && (
        <div className={`rounded-lg px-3 py-2 text-xs ${feedback.tone === "success" ? "bg-green-500/10 text-green-400 border border-green-500/20" : "bg-red-500/10 text-red-400 border border-red-500/20"}`}>
          {feedback.message}
        </div>
      )}
      <button onClick={handleSubmit} disabled={submitting} className={`w-full py-2.5 rounded-lg text-sm font-semibold transition-colors disabled:opacity-50 ${orderSide === "buy" ? "bg-green-500/20 hover:bg-green-500/30 text-green-400 border border-green-500/30" : "bg-red-500/20 hover:bg-red-500/30 text-red-400 border border-red-500/30"}`}>
        {submitting ? "提交中…" : orderSide === "buy" ? "买入下单" : "卖出下单"}
      </button>
    </div>
  );
}

function PortfolioTab({ balances }: { balances: CryptoBalance[] }) {
  if (balances.length === 0) return <div className="text-center py-12 text-[#8b949e] text-sm">暂无持仓数据</div>;
  return (
    <div className="overflow-auto">
      <Table>
        <TableHeader>
          <TableRow className="border-[#2a2e39] hover:bg-transparent">
            <TableHead className="text-[#8b949e] text-xs">资产</TableHead>
            <TableHead className="text-[#8b949e] text-xs text-right">可用</TableHead>
            <TableHead className="text-[#8b949e] text-xs text-right">冻结</TableHead>
            <TableHead className="text-[#8b949e] text-xs text-right">总量</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {balances.map((b) => (
            <TableRow key={b.asset} className="border-[#2a2e39] hover:bg-white/5">
              <TableCell className="text-white text-sm font-medium">{b.asset}</TableCell>
              <TableCell className="text-right font-mono text-sm text-[#8b949e]">{b.free.toFixed(6)}</TableCell>
              <TableCell className="text-right font-mono text-sm text-[#8b949e]">{b.locked.toFixed(6)}</TableCell>
              <TableCell className="text-right font-mono text-sm text-white">{b.total.toFixed(6)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function OrdersTab({ orders, onCancel }: { orders: CryptoOrder[]; onCancel: (orderId: string, symbol: string) => void }) {
  if (orders.length === 0) return <div className="text-center py-12 text-[#8b949e] text-sm">暂无挂单</div>;
  return (
    <div className="overflow-auto">
      <Table>
        <TableHeader>
          <TableRow className="border-[#2a2e39] hover:bg-transparent">
            <TableHead className="text-[#8b949e] text-xs">交易对</TableHead>
            <TableHead className="text-[#8b949e] text-xs">方向</TableHead>
            <TableHead className="text-[#8b949e] text-xs text-right">数量</TableHead>
            <TableHead className="text-[#8b949e] text-xs text-right">价格</TableHead>
            <TableHead className="text-[#8b949e] text-xs">状态</TableHead>
            <TableHead className="text-[#8b949e] text-xs text-center">操作</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {orders.map((o) => (
            <TableRow key={o.order_id} className="border-[#2a2e39] hover:bg-white/5">
              <TableCell className="text-white text-sm font-medium">{o.display}</TableCell>
              <TableCell><span className={`text-xs font-medium ${o.side === "buy" ? "text-green-400" : "text-red-400"}`}>{o.side === "buy" ? "买入" : "卖出"}</span></TableCell>
              <TableCell className="text-right font-mono text-sm text-[#8b949e]">{o.quantity}</TableCell>
              <TableCell className="text-right font-mono text-sm text-[#8b949e]">{o.submitted_price != null ? fmt(o.submitted_price) : "市价"}</TableCell>
              <TableCell><span className="text-xs text-[#8b949e]">{o.status}</span></TableCell>
              <TableCell className="text-center">
                <button onClick={() => onCancel(o.order_id, o.symbol)} className="text-xs px-2 py-1 rounded border border-[#2a2e39] text-[#8b949e] hover:border-red-500/50 hover:text-red-400 transition-colors">撤单</button>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

// ─── Futures Sub-components ───────────────────────────────────────────────────

function FuturesMarketSection({ tickers, selected, onSelect }: { tickers: SwapTicker[]; selected: string; onSelect: (id: string) => void }) {
  if (tickers.length === 0) return <div className="text-center py-8 text-[#8b949e] text-sm">加载合约行情中…</div>;
  return (
    <div className="overflow-auto">
      <Table>
        <TableHeader>
          <TableRow className="border-[#2a2e39] hover:bg-transparent">
            <TableHead className="text-[#8b949e] text-xs">合约</TableHead>
            <TableHead className="text-[#8b949e] text-xs text-right">最新价</TableHead>
            <TableHead className="text-[#8b949e] text-xs text-right">24H涨跌</TableHead>
            <TableHead className="text-[#8b949e] text-xs text-right">资金费率</TableHead>
            <TableHead className="text-[#8b949e] text-xs text-right">持仓量</TableHead>
            <TableHead className="text-[#8b949e] text-xs text-right">24H高</TableHead>
            <TableHead className="text-[#8b949e] text-xs text-right">24H低</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {tickers.map((t) => (
            <TableRow key={t.inst_id} onClick={() => onSelect(t.inst_id)} className={`border-[#2a2e39] cursor-pointer transition-colors ${t.inst_id === selected ? "bg-[#2962ff]/10" : "hover:bg-white/5"}`}>
              <TableCell className="text-white text-sm font-medium">{t.display}</TableCell>
              <TableCell className="text-right font-mono text-sm text-white">${fmt(t.last)}</TableCell>
              <TableCell className="text-right">
                <span className={`text-xs font-medium ${t.change_pct >= 0 ? "text-green-400" : "text-red-400"}`}>{fmtPct(t.change_pct)}</span>
              </TableCell>
              <TableCell className="text-right font-mono text-xs text-[#8b949e]">{fmtFunding(t.funding_rate)}</TableCell>
              <TableCell className="text-right font-mono text-xs text-[#8b949e]">{t.open_interest.toFixed(0)}</TableCell>
              <TableCell className="text-right font-mono text-xs text-[#8b949e]">{fmt(t.high_24h)}</TableCell>
              <TableCell className="text-right font-mono text-xs text-[#8b949e]">{fmt(t.low_24h)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function FuturesTradeForm({ configured, selectedSwap }: { configured: boolean; selectedSwap: string }) {
  const {
    futuresSide, futuresPosSide, futuresOrderType, futuresQty, futuresPrice,
    futuresLever, futuresMarginMode,
    setFuturesSide, setFuturesPosSide, setFuturesOrderType, setFuturesQty,
    setFuturesPrice, setFuturesLever, setFuturesMarginMode,
    setSwapOpenOrders,
  } = useCryptoStore();
  const [submitting, setSubmitting] = useState(false);
  const [feedback, setFeedback] = useState<{ tone: "success" | "error"; message: string } | null>(null);

  if (!configured) {
    return (
      <div className="text-center py-8 space-y-3">
        <div className="text-[#8b949e] text-sm">需要配置 OKX API Key 才能开仓</div>
        <div className="text-xs text-[#434651] font-mono">在 backend/.env 设置 OKX 密钥</div>
      </div>
    );
  }

  async function handleSubmit() {
    if (!futuresQty || parseFloat(futuresQty) <= 0) { setFeedback({ tone: "error", message: "请输入有效的数量（张数）" }); return; }
    if (futuresOrderType === "limit" && (!futuresPrice || parseFloat(futuresPrice) <= 0)) { setFeedback({ tone: "error", message: "限价单需要输入价格" }); return; }
    setSubmitting(true); setFeedback(null);
    try {
      await submitFuturesOrder({
        inst_id: selectedSwap, side: futuresSide, order_type: futuresOrderType,
        sz: parseFloat(futuresQty), pos_side: futuresPosSide,
        margin_mode: futuresMarginMode, lever: futuresLever,
        ...(futuresOrderType === "limit" ? { price: parseFloat(futuresPrice) } : {}),
      });
      setFeedback({ tone: "success", message: "开仓成功！" });
      setFuturesQty(""); setFuturesPrice("");
      getFuturesOpenOrders().then(setSwapOpenOrders).catch(() => {});
    } catch (e) {
      setFeedback({ tone: "error", message: String(e) });
    } finally { setSubmitting(false); }
  }

  return (
    <div className="max-w-sm space-y-4">
      <div className="text-sm text-[#8b949e]">合约：<span className="text-white font-medium">{selectedSwap}</span></div>

      {/* 开多 / 开空 */}
      <div className="flex rounded-lg overflow-hidden border border-[#2a2e39]">
        {([["buy", "long", "开多"], ["sell", "short", "开空"]] as const).map(([side, pos, label]) => (
          <button key={side} onClick={() => { setFuturesSide(side); setFuturesPosSide(pos); }}
            className={`flex-1 py-2 text-sm font-medium transition-colors ${futuresSide === side ? (side === "buy" ? "bg-green-500/20 text-green-400" : "bg-red-500/20 text-red-400") : "text-[#8b949e] hover:text-white"}`}>
            {label}
          </button>
        ))}
      </div>

      {/* 订单类型 & 保证金模式 */}
      <div className="flex gap-3">
        <div className="flex-1 space-y-1">
          <label className="text-xs text-[#8b949e]">订单类型</label>
          <div className="flex gap-1">
            {(["market", "limit"] as const).map((t) => (
              <button key={t} onClick={() => setFuturesOrderType(t)} className={`flex-1 py-1.5 rounded-lg text-xs font-medium border transition-colors ${futuresOrderType === t ? "bg-[#2962ff]/20 border-[#2962ff] text-[#2962ff]" : "border-[#2a2e39] text-[#8b949e] hover:text-white"}`}>
                {t === "market" ? "市价" : "限价"}
              </button>
            ))}
          </div>
        </div>
        <div className="flex-1 space-y-1">
          <label className="text-xs text-[#8b949e]">保证金模式</label>
          <div className="flex gap-1">
            {(["cross", "isolated"] as const).map((m) => (
              <button key={m} onClick={() => setFuturesMarginMode(m)} className={`flex-1 py-1.5 rounded-lg text-xs font-medium border transition-colors ${futuresMarginMode === m ? "bg-[#2962ff]/20 border-[#2962ff] text-[#2962ff]" : "border-[#2a2e39] text-[#8b949e] hover:text-white"}`}>
                {m === "cross" ? "全仓" : "逐仓"}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* 杠杆 */}
      <div className="space-y-1">
        <label className="text-xs text-[#8b949e]">杠杆倍数</label>
        <div className="flex gap-2 flex-wrap">
          {["1", "3", "5", "10", "20", "50"].map((l) => (
            <button key={l} onClick={() => setFuturesLever(l)} className={`px-3 py-1 rounded text-xs font-mono border transition-colors ${futuresLever === l ? "bg-[#2962ff]/20 border-[#2962ff] text-[#2962ff]" : "border-[#2a2e39] text-[#8b949e] hover:text-white"}`}>
              {l}×
            </button>
          ))}
        </div>
      </div>

      {/* 数量（张） */}
      <div className="space-y-1">
        <label className="text-xs text-[#8b949e]">数量（张）</label>
        <input type="number" step="1" min="1" placeholder="1" value={futuresQty} onChange={(e) => setFuturesQty(e.target.value)} className="w-full bg-[#1e222d] border border-[#2a2e39] rounded-lg px-3 py-2 text-white text-sm font-mono focus:outline-none focus:border-[#2962ff]" />
      </div>

      {futuresOrderType === "limit" && (
        <div className="space-y-1">
          <label className="text-xs text-[#8b949e]">限价</label>
          <input type="number" step="0.01" min="0" placeholder="0.00" value={futuresPrice} onChange={(e) => setFuturesPrice(e.target.value)} className="w-full bg-[#1e222d] border border-[#2a2e39] rounded-lg px-3 py-2 text-white text-sm font-mono focus:outline-none focus:border-[#2962ff]" />
        </div>
      )}

      {feedback && (
        <div className={`rounded-lg px-3 py-2 text-xs ${feedback.tone === "success" ? "bg-green-500/10 text-green-400 border border-green-500/20" : "bg-red-500/10 text-red-400 border border-red-500/20"}`}>
          {feedback.message}
        </div>
      )}

      <button onClick={handleSubmit} disabled={submitting} className={`w-full py-2.5 rounded-lg text-sm font-semibold transition-colors disabled:opacity-50 ${futuresSide === "buy" ? "bg-green-500/20 hover:bg-green-500/30 text-green-400 border border-green-500/30" : "bg-red-500/20 hover:bg-red-500/30 text-red-400 border border-red-500/30"}`}>
        {submitting ? "提交中…" : futuresSide === "buy" ? `开多 ${futuresLever}× ${selectedSwap}` : `开空 ${futuresLever}× ${selectedSwap}`}
      </button>
    </div>
  );
}

function PositionsTable({ positions }: { positions: CryptoPosition[] }) {
  if (positions.length === 0) return <div className="text-center py-12 text-[#8b949e] text-sm">暂无持仓</div>;
  return (
    <div className="overflow-auto">
      <Table>
        <TableHeader>
          <TableRow className="border-[#2a2e39] hover:bg-transparent">
            <TableHead className="text-[#8b949e] text-xs">合约</TableHead>
            <TableHead className="text-[#8b949e] text-xs">方向</TableHead>
            <TableHead className="text-[#8b949e] text-xs text-right">数量</TableHead>
            <TableHead className="text-[#8b949e] text-xs text-right">开仓均价</TableHead>
            <TableHead className="text-[#8b949e] text-xs text-right">标记价格</TableHead>
            <TableHead className="text-[#8b949e] text-xs text-right">未实现盈亏</TableHead>
            <TableHead className="text-[#8b949e] text-xs text-right">杠杆</TableHead>
            <TableHead className="text-[#8b949e] text-xs text-right">强平价</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {positions.map((p) => (
            <TableRow key={p.pos_id || p.inst_id} className="border-[#2a2e39] hover:bg-white/5">
              <TableCell className="text-white text-sm font-medium">{p.inst_id}</TableCell>
              <TableCell>
                <span className={`text-xs font-medium px-1.5 py-0.5 rounded ${p.pos_side === "long" ? "bg-green-400/10 text-green-400" : "bg-red-400/10 text-red-400"}`}>
                  {p.pos_side === "long" ? "多" : p.pos_side === "short" ? "空" : "净"}
                </span>
              </TableCell>
              <TableCell className="text-right font-mono text-sm text-[#8b949e]">{p.pos}</TableCell>
              <TableCell className="text-right font-mono text-sm text-[#8b949e]">{fmt(p.avg_px)}</TableCell>
              <TableCell className="text-right font-mono text-sm text-white">{fmt(p.mark_px)}</TableCell>
              <TableCell className="text-right font-mono text-sm">
                <span className={p.upl >= 0 ? "text-green-400" : "text-red-400"}>
                  {p.upl >= 0 ? "+" : ""}{p.upl.toFixed(4)}
                  <span className="text-xs ml-1 opacity-70">({(p.upl_ratio * 100).toFixed(2)}%)</span>
                </span>
              </TableCell>
              <TableCell className="text-right font-mono text-sm text-[#8b949e]">{p.lever}×</TableCell>
              <TableCell className="text-right font-mono text-xs text-red-400/70">{p.liq_px ? fmt(p.liq_px) : "—"}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

// ─── Chart Tab ───────────────────────────────────────────────────────────────

const TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d", "1w"] as const;

function CryptoChartTab({ selectedSymbol }: { selectedSymbol: string }) {
  const { chartTimeframe, setChartTimeframe } = useCryptoStore();
  const [bars, setBars] = useState<OhlcBar[]>([]);
  const [loading, setLoading] = useState(false);
  const [fetching, setFetching] = useState(false);
  const [chartError, setChartError] = useState<string | null>(null);

  useEffect(() => {
    void loadBars();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedSymbol, chartTimeframe]);

  async function loadBars() {
    setLoading(true);
    setChartError(null);
    try {
      const r = await fetch(
        `/api/data/bars?symbol=${encodeURIComponent(selectedSymbol)}&timeframe=${chartTimeframe}&limit=500`
      );
      const d = await r.json() as { bars?: OhlcBar[] };
      setBars(d.bars ?? []);
    } catch (e) {
      setChartError(String(e));
    } finally {
      setLoading(false);
    }
  }

  async function handleFetch() {
    setFetching(true);
    setChartError(null);
    try {
      await fetch("/api/data/fetch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol: selectedSymbol,
          timeframe: chartTimeframe,
          start: "2020-01-01",
          source: "auto",
        }),
      });
      await new Promise((r) => setTimeout(r, 3000));
      await loadBars();
    } catch (e) {
      setChartError(String(e));
    } finally {
      setFetching(false);
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <span className="text-xs text-[#8b949e]">{selectedSymbol}</span>
        <div className="flex gap-1 ml-2">
          {TIMEFRAMES.map((tf) => (
            <button
              key={tf}
              onClick={() => setChartTimeframe(tf)}
              className={`px-2 py-1 text-xs rounded font-mono border transition-colors ${
                chartTimeframe === tf
                  ? "bg-[#2962ff]/20 border-[#2962ff] text-[#2962ff]"
                  : "border-[#2a2e39] text-[#8b949e] hover:text-white"
              }`}
            >
              {tf}
            </button>
          ))}
        </div>
        <div className="flex-1" />
        <button
          onClick={() => void loadBars()}
          disabled={loading}
          className="px-3 py-1 text-xs text-[#8b949e] hover:text-white border border-[#2a2e39] rounded disabled:opacity-50"
        >
          {loading ? "加载中…" : "刷新"}
        </button>
      </div>

      {chartError && (
        <div className="px-3 py-2 rounded-lg bg-red-500/10 border border-red-500/20 text-red-400 text-xs">{chartError}</div>
      )}

      {bars.length > 0 ? (
        <CandlestickChart
          bars={bars}
          indicatorData={{}}
          activeOverlays={new Set()}
          showVolume={true}
          height={460}
        />
      ) : (
        <div className="flex flex-col items-center justify-center py-16 gap-4">
          <div className="text-[#8b949e] text-sm">
            {loading ? "加载中…" : `暂无 ${selectedSymbol} ${chartTimeframe} 数据`}
          </div>
          {!loading && (
            <button
              onClick={() => void handleFetch()}
              disabled={fetching}
              className="px-4 py-2 text-sm text-white bg-[#2962ff]/20 hover:bg-[#2962ff]/30 border border-[#2962ff]/40 rounded-lg disabled:opacity-50 transition-colors"
            >
              {fetching ? "拉取中（约 3~10 秒）…" : "从 OKX 拉取历史数据"}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Backtest Tab ─────────────────────────────────────────────────────────────

interface AvailableStrategy {
  id: string;
  name: string;
  description: string;
  default_params: Record<string, unknown>;
}

interface BacktestResult {
  symbol: string;
  timeframe: string;
  strategy_id: string;
  bars_processed: number;
  trades_count: number;
  metrics: Record<string, number | string>;
}

function CryptoBacktestTab({ selectedSymbol }: { selectedSymbol: string }) {
  const [strategies, setStrategies] = useState<AvailableStrategy[]>([]);
  const [strategyId, setStrategyId] = useState("");
  const [timeframe, setTimeframe] = useState("1h");
  const [startDate, setStartDate] = useState("2023-01-01");
  const [endDate, setEndDate] = useState(new Date().toISOString().slice(0, 10));
  const [initialCash, setInitialCash] = useState("10000");
  const [commission, setCommission] = useState("0.001");
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [btError, setBtError] = useState<string | null>(null);
  const [status, setStatus] = useState<string>("");

  useEffect(() => {
    fetch("/api/portfolio/available-strategies")
      .then((r) => r.json() as Promise<{ strategies: AvailableStrategy[] }>)
      .then((d) => {
        const ordered = prioritizeCryptoStrategies(d.strategies ?? []);
        setStrategies(ordered);
        if (ordered.length > 0 && !strategyId) {
          const defaultStrategy = defaultCryptoBacktestStrategy(ordered);
          setStrategyId(defaultStrategy);
          setTimeframe(recommendedCryptoBacktestTimeframe(defaultStrategy));
        }
      })
      .catch(() => {});
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!strategyId) {
      return;
    }
    setTimeframe((current) => (current === "1d" ? recommendedCryptoBacktestTimeframe(strategyId) : current));
  }, [strategyId]);

  async function handleRun() {
    if (!strategyId) { setBtError("请选择策略"); return; }
    setRunning(true); setBtError(null); setResult(null);

    try {
      setStatus("正在获取历史数据…");
      const barsResp = await fetch(
        `/api/data/bars?symbol=${encodeURIComponent(selectedSymbol)}&timeframe=${timeframe}&limit=1000&start=${startDate}&end=${endDate}`
      ).then((r) => r.json() as Promise<{ bars: OhlcBar[] }>);

      let bars = barsResp.bars ?? [];
      if (bars.length < 2) {
        setStatus("数据不足，正在从 OKX 拉取…");
        await fetch("/api/data/fetch", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ symbol: selectedSymbol, timeframe, start: startDate, source: "auto" }),
        });
        await new Promise((r) => setTimeout(r, 4000));
        const re = await fetch(
          `/api/data/bars?symbol=${encodeURIComponent(selectedSymbol)}&timeframe=${timeframe}&limit=1000&start=${startDate}&end=${endDate}`
        ).then((r) => r.json() as Promise<{ bars: OhlcBar[] }>);
        bars = re.bars ?? [];
      }
      if (bars.length < 2) { setBtError("数据不足，无法回测（至少需要 2 根 K 线）"); return; }

      setStatus(`运行回测（${bars.length} 根 K 线）…`);
      const selected = strategies.find((s) => s.id === strategyId);
      const backtestResp = await fetch("/api/backtest/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol: selectedSymbol,
          timeframe,
          bars,
          strategy_id: strategyId,
          strategy_params: selected?.default_params ?? {},
          initial_cash: parseFloat(initialCash),
          commission_rate: parseFloat(commission),
          slippage_pct: 0.0005,
          stop_loss_pct: null,
          take_profit_pct: null,
        }),
      });
      if (!backtestResp.ok) {
        const text = await backtestResp.text();
        throw new Error(text);
      }
      setResult(await backtestResp.json() as BacktestResult);
    } catch (e) {
      setBtError(String(e));
    } finally {
      setRunning(false); setStatus("");
    }
  }

  const pct = (v: unknown) => v != null ? `${(Number(v) * 100).toFixed(2)}%` : "—";
  const num = (v: unknown, d = 4) => v != null ? Number(v).toFixed(d) : "—";

  return (
    <div className="flex gap-6">
      <div className="w-64 flex-shrink-0 space-y-4">
        <div className="space-y-1">
          <label className="text-xs text-[#8b949e]">交易对</label>
          <div className="px-3 py-2 bg-[#1e222d] border border-[#2a2e39] rounded-lg text-white text-sm font-mono">{selectedSymbol}</div>
        </div>

        <div className="space-y-1">
          <label className="text-xs text-[#8b949e]">策略</label>
          <select value={strategyId} onChange={(e) => setStrategyId(e.target.value)}
            className="w-full bg-[#1e222d] border border-[#2a2e39] rounded-lg px-3 py-2 text-white text-sm focus:outline-none focus:border-[#2962ff]">
            {strategies.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        </div>
        {strategyId === "vwap_ema_trend" ? (
          <div className="rounded-lg border border-[#2962ff]/30 bg-[#2962ff]/10 p-3 text-xs text-[#c9d1d9] space-y-1">
            <div className="font-medium text-white">默认研究策略</div>
            <div>推荐周期：15m / 1h</div>
            <div>参数关注：fast_period / slow_period / vwap_window / trailing_stop_pct / max_hold_bars</div>
          </div>
        ) : null}

        <div className="space-y-1">
          <label className="text-xs text-[#8b949e]">K 线周期</label>
          <div className="flex flex-wrap gap-1">
            {(["1h", "4h", "1d", "1w"] as const).map((tf) => (
              <button key={tf} onClick={() => setTimeframe(tf)}
                className={`px-2 py-1 text-xs rounded border font-mono transition-colors ${timeframe === tf ? "bg-[#2962ff]/20 border-[#2962ff] text-[#2962ff]" : "border-[#2a2e39] text-[#8b949e] hover:text-white"}`}>
                {tf}
              </button>
            ))}
          </div>
        </div>

        <div className="grid grid-cols-2 gap-2">
          <div className="space-y-1">
            <label className="text-xs text-[#8b949e]">开始日期</label>
            <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)}
              className="w-full bg-[#1e222d] border border-[#2a2e39] rounded-lg px-2 py-1.5 text-white text-xs focus:outline-none focus:border-[#2962ff]" />
          </div>
          <div className="space-y-1">
            <label className="text-xs text-[#8b949e]">结束日期</label>
            <input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)}
              className="w-full bg-[#1e222d] border border-[#2a2e39] rounded-lg px-2 py-1.5 text-white text-xs focus:outline-none focus:border-[#2962ff]" />
          </div>
        </div>

        <div className="grid grid-cols-2 gap-2">
          <div className="space-y-1">
            <label className="text-xs text-[#8b949e]">本金 (USDT)</label>
            <input type="number" value={initialCash} onChange={(e) => setInitialCash(e.target.value)}
              className="w-full bg-[#1e222d] border border-[#2a2e39] rounded-lg px-3 py-2 text-white text-sm font-mono focus:outline-none focus:border-[#2962ff]" />
          </div>
          <div className="space-y-1">
            <label className="text-xs text-[#8b949e]">手续费率</label>
            <input type="number" step="0.0001" value={commission} onChange={(e) => setCommission(e.target.value)}
              className="w-full bg-[#1e222d] border border-[#2a2e39] rounded-lg px-3 py-2 text-white text-sm font-mono focus:outline-none focus:border-[#2962ff]" />
          </div>
        </div>

        {btError && <div className="px-3 py-2 rounded-lg bg-red-500/10 border border-red-500/20 text-red-400 text-xs">{btError}</div>}
        {status && <div className="text-xs text-[#8b949e]">{status}</div>}

        <button onClick={() => void handleRun()} disabled={running || !strategyId}
          className="w-full py-2.5 rounded-lg text-sm font-semibold bg-[#2962ff]/20 hover:bg-[#2962ff]/30 text-[#2962ff] border border-[#2962ff]/40 transition-colors disabled:opacity-50">
          {running ? "回测中…" : "运行回测"}
        </button>
      </div>

      <div className="flex-1">
        {!result ? (
          <div className="flex items-center justify-center h-full text-[#8b949e] text-sm py-16">
            {running ? "回测运行中，请稍候…" : "配置参数后点击「运行回测」"}
          </div>
        ) : (
          <div className="space-y-4">
            <div className="text-xs text-[#8b949e]">
              {result.symbol} · {result.strategy_id} · {result.bars_processed} 根 K 线 · {result.trades_count} 笔交易
            </div>
            <div className="grid grid-cols-3 gap-3">
              {[
                { label: "总收益率", value: pct(result.metrics.total_return), color: Number(result.metrics.total_return) >= 0 ? "text-green-400" : "text-red-400" },
                { label: "年化收益", value: pct(result.metrics.annual_return), color: Number(result.metrics.annual_return) >= 0 ? "text-green-400" : "text-red-400" },
                { label: "最大回撤", value: pct(result.metrics.max_drawdown), color: "text-red-400" },
                { label: "夏普比率", value: num(result.metrics.sharpe_ratio, 2), color: "text-white" },
                { label: "胜率", value: pct(result.metrics.win_rate), color: "text-white" },
                { label: "交易次数", value: String(result.trades_count), color: "text-white" },
              ].map(({ label, value, color }) => (
                <div key={label} className="bg-[#1e222d] border border-[#2a2e39] rounded-xl p-3">
                  <div className="text-xs text-[#434651] mb-1">{label}</div>
                  <div className={`text-lg font-bold font-mono ${color}`}>{value}</div>
                </div>
              ))}
            </div>
            <div className="bg-[#1e222d] border border-[#2a2e39] rounded-xl p-4">
              <div className="text-xs text-[#8b949e] mb-3 font-medium">详细指标</div>
              <div className="grid grid-cols-2 gap-x-8 gap-y-2 text-xs">
                {[
                  ["Sortino 比率", num(result.metrics.sortino_ratio, 2)],
                  ["Calmar 比率", num(result.metrics.calmar_ratio, 2)],
                  ["波动率", pct(result.metrics.volatility)],
                  ["盈亏比", num(result.metrics.profit_factor, 2)],
                  ["平均盈利", pct(result.metrics.avg_win)],
                  ["平均亏损", pct(result.metrics.avg_loss)],
                  ["最终净值", `$${Number(result.metrics.final_value ?? 0).toFixed(2)}`],
                  ["净利润", `$${Number(result.metrics.net_profit ?? 0).toFixed(2)}`],
                ].map(([k, v]) => (
                  <div key={k} className="flex justify-between">
                    <span className="text-[#434651]">{k}</span>
                    <span className="font-mono text-[#8b949e]">{v}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Futures Tab ──────────────────────────────────────────────────────────────

function FuturesTab({ configured }: { configured: boolean }) {
  const {
    swapTickers, swapPositions, swapOpenOrders, selectedSwap,
    setSwapTickers, setSwapPositions, setSwapOpenOrders, setSelectedSwap,
    setError,
  } = useCryptoStore();
  const [subTab, setSubTab] = useState<"market" | "trade" | "positions" | "orders">("market");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    void loadFutures();
  }, []);

  async function loadFutures() {
    setLoading(true);
    try {
      const tickers = await getFuturesTickers();
      setSwapTickers(tickers);
      if (tickers.length > 0 && !tickers.find(t => t.inst_id === selectedSwap)) {
        setSelectedSwap(tickers[0].inst_id);
      }
      if (configured) {
        const [pos, orders] = await Promise.all([
          getFuturesPositions().catch(() => []),
          getFuturesOpenOrders().catch(() => []),
        ]);
        setSwapPositions(pos);
        setSwapOpenOrders(orders);
      }
    } catch (e) {
      setError(`合约行情加载失败: ${String(e)}`);
    } finally {
      setLoading(false);
    }
  }

  async function handleCancelFutures(orderId: string, instId: string) {
    try {
      await cancelFuturesOrder(orderId, instId);
      getFuturesOpenOrders().then(setSwapOpenOrders).catch(() => {});
    } catch (e) {
      setError(String(e));
    }
  }

  const subTabs = [
    { key: "market", label: "行情" },
    { key: "trade", label: "交易" },
    { key: "positions", label: `持仓${swapPositions.length > 0 ? ` (${swapPositions.length})` : ""}` },
    { key: "orders", label: `挂单${swapOpenOrders.length > 0 ? ` (${swapOpenOrders.length})` : ""}` },
  ] as const;

  return (
    <div className="space-y-4">
      {/* 子标签 */}
      <div className="flex gap-1 border-b border-[#2a2e39] pb-0">
        {subTabs.map((t) => (
          <button key={t.key} onClick={() => setSubTab(t.key)}
            className={`px-3 py-1.5 text-xs font-medium border-b-2 transition-colors -mb-px ${subTab === t.key ? "border-[#2962ff] text-white" : "border-transparent text-[#8b949e] hover:text-white"}`}>
            {t.label}
          </button>
        ))}
        <div className="flex-1" />
        <button onClick={() => void loadFutures()} disabled={loading} className="px-3 py-1 text-xs text-[#8b949e] hover:text-white border border-[#2a2e39] rounded mb-1 disabled:opacity-50">
          {loading ? "加载中…" : "刷新"}
        </button>
      </div>

      {subTab === "market" && (
        <FuturesMarketSection tickers={swapTickers} selected={selectedSwap} onSelect={setSelectedSwap} />
      )}
      {subTab === "trade" && (
        <div className="flex gap-6">
          <FuturesTradeForm configured={configured} selectedSwap={selectedSwap} />
          {/* 右侧快速行情 */}
          {swapTickers.find(t => t.inst_id === selectedSwap) && (() => {
            const t = swapTickers.find(x => x.inst_id === selectedSwap)!;
            return (
              <div className="flex-1 max-w-xs space-y-3 pt-8">
                <div className="bg-[#1e222d] border border-[#2a2e39] rounded-xl p-4 space-y-3">
                  <div className="flex items-center justify-between">
                    <span className="text-sm text-white font-medium">{t.display}</span>
                    <span className={`text-xs px-1.5 py-0.5 rounded ${t.change_pct >= 0 ? "bg-green-400/10 text-green-400" : "bg-red-400/10 text-red-400"}`}>{fmtPct(t.change_pct)}</span>
                  </div>
                  <div className="text-2xl font-bold font-mono text-white">${fmt(t.last)}</div>
                  <div className="grid grid-cols-2 gap-2 text-xs">
                    <div><div className="text-[#434651]">资金费率</div><div className="font-mono text-[#8b949e]">{fmtFunding(t.funding_rate)}</div></div>
                    <div><div className="text-[#434651]">持仓量</div><div className="font-mono text-[#8b949e]">{t.open_interest.toFixed(0)}</div></div>
                    <div><div className="text-[#434651]">24H高</div><div className="font-mono text-[#8b949e]">{fmt(t.high_24h)}</div></div>
                    <div><div className="text-[#434651]">24H低</div><div className="font-mono text-[#8b949e]">{fmt(t.low_24h)}</div></div>
                  </div>
                </div>
              </div>
            );
          })()}
        </div>
      )}
      {subTab === "positions" && <PositionsTable positions={swapPositions} />}
      {subTab === "orders" && (
        <OrdersTab orders={swapOpenOrders} onCancel={handleCancelFutures} />
      )}
    </div>
  );
}

// ─── Options Tab ──────────────────────────────────────────────────────────────

function OptionsChainTable({ chain }: { chain: OptionTicker[] }) {
  if (chain.length === 0) return <div className="text-center py-12 text-[#8b949e] text-sm">暂无期权数据</div>;

  // 按行权价分组，左边 call，右边 put
  const strikeMap = new Map<number, { call?: OptionTicker; put?: OptionTicker }>();
  for (const t of chain) {
    const entry = strikeMap.get(t.strike_px) ?? {};
    if (t.opt_type === "C") entry.call = t;
    else entry.put = t;
    strikeMap.set(t.strike_px, entry);
  }
  const strikes = [...strikeMap.keys()].sort((a, b) => a - b);

  const cellCls = "font-mono text-xs text-right text-[#8b949e]";
  const headerCls = "text-[#8b949e] text-xs text-right";

  return (
    <div className="overflow-auto">
      <Table>
        <TableHeader>
          <TableRow className="border-[#2a2e39] hover:bg-transparent">
            {/* Call 列 */}
            <TableHead className={`${headerCls} text-green-400/70`}>最新价</TableHead>
            <TableHead className={`${headerCls} text-green-400/70`}>买价</TableHead>
            <TableHead className={`${headerCls} text-green-400/70`}>卖价</TableHead>
            <TableHead className={`${headerCls} text-green-400/70`}>持仓量</TableHead>
            {/* 行权价 */}
            <TableHead className="text-[#8b949e] text-xs text-center font-semibold">行权价 (认购↑ | 认沽↓)</TableHead>
            {/* Put 列 */}
            <TableHead className={`${headerCls} text-red-400/70`}>持仓量</TableHead>
            <TableHead className={`${headerCls} text-red-400/70`}>买价</TableHead>
            <TableHead className={`${headerCls} text-red-400/70`}>卖价</TableHead>
            <TableHead className={`${headerCls} text-red-400/70`}>最新价</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {strikes.map((strike) => {
            const { call, put } = strikeMap.get(strike)!;
            return (
              <TableRow key={strike} className="border-[#2a2e39] hover:bg-white/5">
                {/* Call */}
                <TableCell className={`${cellCls} text-green-400`}>{call?.last ? fmt(call.last) : "—"}</TableCell>
                <TableCell className={`${cellCls} text-green-400`}>{call?.bid_px ? fmt(call.bid_px) : "—"}</TableCell>
                <TableCell className={`${cellCls} text-green-400`}>{call?.ask_px ? fmt(call.ask_px) : "—"}</TableCell>
                <TableCell className={cellCls}>{call?.open_interest ? call.open_interest.toFixed(0) : "—"}</TableCell>
                {/* Strike */}
                <TableCell className="text-center font-mono text-sm font-semibold text-white">{strike.toLocaleString()}</TableCell>
                {/* Put */}
                <TableCell className={cellCls}>{put?.open_interest ? put.open_interest.toFixed(0) : "—"}</TableCell>
                <TableCell className={`${cellCls} text-red-400`}>{put?.bid_px ? fmt(put.bid_px) : "—"}</TableCell>
                <TableCell className={`${cellCls} text-red-400`}>{put?.ask_px ? fmt(put.ask_px) : "—"}</TableCell>
                <TableCell className={`${cellCls} text-red-400`}>{put?.last ? fmt(put.last) : "—"}</TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </div>
  );
}

function OptionsTab({ configured }: { configured: boolean }) {
  const {
    optionUnderlying, optionExpiries, optionSelectedExpiry, optionChain, optionPositions,
    setOptionUnderlying, setOptionExpiries, setOptionSelectedExpiry, setOptionChain, setOptionPositions,
    setError,
  } = useCryptoStore();
  const [subTab, setSubTab] = useState<"chain" | "positions">("chain");
  const [loading, setLoading] = useState(false);
  const [underlyings, setUnderlyings] = useState<string[]>([]);

  useEffect(() => {
    void loadUnderlyings();
  }, []);

  useEffect(() => {
    if (optionUnderlying) void loadExpiries(optionUnderlying);
  }, [optionUnderlying]);

  useEffect(() => {
    if (optionUnderlying && optionSelectedExpiry) void loadChain(optionUnderlying, optionSelectedExpiry);
  }, [optionUnderlying, optionSelectedExpiry]);

  async function loadUnderlyings() {
    try {
      const list = await getOptionsUnderlyings();
      setUnderlyings(list);
    } catch { /* ignore */ }
  }

  async function loadExpiries(uly: string) {
    setLoading(true);
    try {
      const exps = await getOptionsExpiries(uly);
      setOptionExpiries(exps);
      if (exps.length > 0 && !exps.includes(optionSelectedExpiry)) {
        setOptionSelectedExpiry(exps[0]);
      }
    } catch (e) {
      setError(`期权到期日加载失败: ${String(e)}`);
    } finally {
      setLoading(false);
    }
  }

  async function loadChain(uly: string, expTime: string) {
    setLoading(true);
    try {
      const chain = await getOptionsChain(uly, expTime);
      setOptionChain(chain);
      if (configured) {
        getOptionsPositions().then(setOptionPositions).catch(() => {});
      }
    } catch (e) {
      setError(`期权链加载失败: ${String(e)}`);
    } finally {
      setLoading(false);
    }
  }

  const selectCls = "bg-[#1e222d] border border-[#2a2e39] rounded-lg px-3 py-1.5 text-white text-xs focus:outline-none focus:border-[#2962ff]";

  return (
    <div className="space-y-4">
      {/* 筛选栏 */}
      <div className="flex items-center gap-3 flex-wrap">
        <div className="flex items-center gap-2">
          <span className="text-xs text-[#8b949e]">标的</span>
          <select value={optionUnderlying} onChange={(e) => setOptionUnderlying(e.target.value)} className={selectCls}>
            {(underlyings.length > 0 ? underlyings : ["BTC-USD", "ETH-USD", "SOL-USD"]).map((u) => (
              <option key={u} value={u}>{u}</option>
            ))}
          </select>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-[#8b949e]">到期日</span>
          <select value={optionSelectedExpiry} onChange={(e) => setOptionSelectedExpiry(e.target.value)} className={selectCls}>
            {optionExpiries.length === 0 ? <option value="">—</option> : optionExpiries.map((e) => (
              <option key={e} value={e}>{e}</option>
            ))}
          </select>
        </div>
        <button onClick={() => { if (optionUnderlying && optionSelectedExpiry) void loadChain(optionUnderlying, optionSelectedExpiry); }} disabled={loading} className="px-3 py-1.5 text-xs text-[#8b949e] hover:text-white border border-[#2a2e39] rounded-lg disabled:opacity-50">
          {loading ? "加载中…" : "刷新"}
        </button>
        <div className="flex-1" />
        {/* 子标签 */}
        <div className="flex gap-1">
          {(["chain", "positions"] as const).map((t) => (
            <button key={t} onClick={() => setSubTab(t)} className={`px-3 py-1 text-xs font-medium border rounded transition-colors ${subTab === t ? "bg-[#2962ff]/20 border-[#2962ff] text-[#2962ff]" : "border-[#2a2e39] text-[#8b949e] hover:text-white"}`}>
              {t === "chain" ? "期权链" : `持仓${optionPositions.length > 0 ? ` (${optionPositions.length})` : ""}`}
            </button>
          ))}
        </div>
      </div>

      {/* 说明行 */}
      {subTab === "chain" && (
        <div className="text-[10px] text-[#434651] flex gap-4">
          <span className="text-green-400/50">绿色列 = 认购(Call)</span>
          <span className="text-red-400/50">红色列 = 认沽(Put)</span>
          <span>中间列 = 行权价</span>
        </div>
      )}

      {subTab === "chain" && <OptionsChainTable chain={optionChain} />}
      {subTab === "positions" && <PositionsTable positions={optionPositions} />}
    </div>
  );
}

// ─── Main Component ───────────────────────────────────────────────────────────

interface CryptoPanelProps {
  allowedTabs?: CryptoActiveTab[];
  defaultTab?: CryptoActiveTab;
}

export default function CryptoPanel({
  allowedTabs,
  defaultTab,
}: CryptoPanelProps = {}) {
  const {
    configured, testnet, pairs, tickers, selectedSymbol, balances, openOrders,
    activeTab, error,
    setConfigured, setPairs, setTickers, setSelectedSymbol, setBalances,
    setOpenOrders, setActiveTab, setError,
  } = useCryptoStore();

  const [refreshing, setRefreshing] = useState(false);

  async function loadAll() {
    setError(null);
    try {
      const [status, fetchedPairs] = await Promise.all([getCryptoStatus(), getCryptoPairs(false)]);
      setConfigured(status.configured, status.testnet);
      setPairs(fetchedPairs);
      if (fetchedPairs.length > 0) {
        try {
          const tks = await getCryptoTicker(fetchedPairs.map((p) => p.symbol));
          setTickers(tks);
        } catch (e) {
          setError(`行情加载失败: ${String(e)}`);
        }
      }
      if (status.configured) {
        getCryptoAccount().then((a) => setBalances(a.balances)).catch(() => {});
        getCryptoOpenOrders().then(setOpenOrders).catch(() => {});
      }
    } catch (e) {
      setError(`初始化失败: ${String(e)}`);
    }
  }

  useEffect(() => { void loadAll(); }, []);

  async function handleRefresh() {
    setRefreshing(true);
    await loadAll();
    setRefreshing(false);
  }

  async function handleCancelOrder(orderId: string, symbol: string) {
    try {
      await cancelCryptoOrder(orderId, symbol);
      getCryptoOpenOrders().then(setOpenOrders).catch(() => {});
    } catch (e) {
      setError(String(e));
    }
  }

  const tabMap: Record<string, string> = {
    market:    "行情",
    trade:     "交易",
    portfolio: "持仓",
    orders:    "订单",
    futures:   "合约",
    options:   "期权",
    chart:     "图表",
    backtest:  "回测",
  };

  const allTabs = ["market", "trade", "portfolio", "orders", "futures", "options", "chart", "backtest"] as const;
  const visibleTabs = allowedTabs ?? [...allTabs];

  useEffect(() => {
    const nextTab = defaultTab ?? visibleTabs[0];
    if (nextTab && !visibleTabs.includes(activeTab)) {
      setActiveTab(nextTab);
    }
  }, [activeTab, defaultTab, setActiveTab, visibleTabs]);

  return (
    <div className="flex h-full bg-[#131722] text-white overflow-hidden">
      {/* ── Left Sidebar ──────────────────────────────────────── */}
      <div className="w-56 flex-shrink-0 border-r border-[#2a2e39] flex flex-col">
        <div className="px-3 py-3 border-b border-[#2a2e39]">
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold text-white">加密货币</span>
            {testnet && (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-yellow-500/20 text-yellow-400 font-medium">TESTNET</span>
            )}
          </div>
          <div className="text-[10px] text-[#434651] mt-0.5">OKX · 现货 / 合约 / 期权</div>
        </div>

        <div className="flex-1 overflow-y-auto py-1">
          {pairs.length === 0 ? (
            <div className="px-3 py-4 text-xs text-[#434651]">加载中…</div>
          ) : (
            pairs.map((pair) => (
              <WatchlistItem key={pair.symbol} pair={pair} selected={pair.symbol === selectedSymbol} ticker={tickers[pair.symbol]} onClick={() => setSelectedSymbol(pair.symbol)} />
            ))
          )}
        </div>

        <div className="p-3 border-t border-[#2a2e39]">
          <button onClick={() => void handleRefresh()} disabled={refreshing} className="w-full px-3 py-2 text-xs text-[#8b949e] hover:text-white border border-[#2a2e39] rounded-lg hover:border-[#2962ff] transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
            {refreshing ? "加载中…" : "刷新行情"}
          </button>
        </div>
      </div>

      {/* ── Right Main Area ───────────────────────────────────── */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {error && (
          <div className="mx-4 mt-3 px-3 py-2 rounded-lg bg-red-500/10 border border-red-500/20 text-red-400 text-xs">{error}</div>
        )}

        <div className="px-4 pt-3 pb-0 border-b border-[#2a2e39]">
          <Tabs value={activeTab} onValueChange={(v) => setActiveTab(v as typeof activeTab)}>
            <TabsList className="bg-transparent border-0 p-0 gap-1 h-auto">
              {visibleTabs.map((tab) => (
                <TabsTrigger key={tab} value={tab}
                  className="px-3 py-2 text-sm rounded-none border-b-2 border-transparent data-[state=active]:border-[#2962ff] data-[state=active]:text-white data-[state=active]:bg-transparent text-[#8b949e] hover:text-white transition-colors">
                  {tabMap[tab]}
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
        </div>

        <div className="flex-1 overflow-y-auto p-4">
          {activeTab === "market" && visibleTabs.includes("market") && <MarketTab tickers={tickers} />}
          {activeTab === "trade" && visibleTabs.includes("trade") && <TradeTab configured={configured} selectedSymbol={selectedSymbol} />}
          {activeTab === "portfolio" && visibleTabs.includes("portfolio") && <PortfolioTab balances={balances} />}
          {activeTab === "orders" && visibleTabs.includes("orders") && <OrdersTab orders={openOrders} onCancel={handleCancelOrder} />}
          {activeTab === "futures" && visibleTabs.includes("futures") && <FuturesTab configured={configured} />}
          {activeTab === "options" && visibleTabs.includes("options") && <OptionsTab configured={configured} />}
          {activeTab === "chart" && visibleTabs.includes("chart") && <CryptoChartTab selectedSymbol={selectedSymbol} />}
          {activeTab === "backtest" && visibleTabs.includes("backtest") && <CryptoBacktestTab selectedSymbol={selectedSymbol} />}
        </div>
      </div>
    </div>
  );
}

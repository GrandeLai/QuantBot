/**
 * 图表快捷下单面板.
 * 统一走 /api/trading 链路，由当前 provider 状态决定实际执行能力。
 */
import { useEffect, useState } from "react";
import { ChevronDown, LoaderCircle, MoreHorizontal, RefreshCw } from "lucide-react";
import FeatureGuideButton from "@/components/guides/FeatureGuideButton";
import { useChartStore } from "../../store/chartStore";
import { cn } from "../../lib/utils";
import {
  estimateTradingOrder,
  fetchTradingQuotes,
  fetchTradingStatus,
  searchTradingSecurities,
  submitTradingOrder,
  type TradingOrderEstimate,
  type TradingOrderType,
  type TradingProviderStatus,
  type TradingQuote,
  type TradingSecurity,
} from "../../api/trading";

export default function ChartOrderEntry() {
  const { symbol } = useChartStore();
  const [selectedSecurity, setSelectedSecurity] = useState<TradingSecurity | null>(null);
  const [providerStatus, setProviderStatus] = useState<TradingProviderStatus | null>(null);
  const [quote, setQuote] = useState<TradingQuote | null>(null);
  const [estimate, setEstimate] = useState<TradingOrderEstimate | null>(null);
  const [qty, setQty] = useState(1);
  const [limitPrice, setLimitPrice] = useState("");
  const [orderType, setOrderType] = useState<TradingOrderType>("market");
  const [submitting, setSubmitting] = useState(false);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    const loadStatus = async () => {
      try {
        setProviderStatus(await fetchTradingStatus());
      } catch {
        // ignore
      }
    };
    void loadStatus();
  }, []);

  useEffect(() => {
    const resolveSecurity = async () => {
      setLoading(true);
      setMessage(null);
      try {
        const results = await searchTradingSecurities(symbol);
        const security = results.find((item) => item.symbol.startsWith(symbol.toUpperCase())) ?? results[0] ?? null;
        setSelectedSecurity(security);
        if (!security) {
          setQuote(null);
          setEstimate(null);
          setMessage("当前图表标的未映射到交易支持列表，请在交易页使用完整代码下单。");
          return;
        }
        const [quoteItem] = await fetchTradingQuotes([security.symbol]);
        setQuote(quoteItem ?? null);
        if (quoteItem) {
          const nextEstimate = await estimateTradingOrder({
            symbol: security.symbol,
            side: "buy",
            order_type: orderType,
            submitted_price: orderType === "limit" && limitPrice ? Number(limitPrice) : undefined,
          });
          setEstimate(nextEstimate);
        }
      } catch (error) {
        setMessage(error instanceof Error ? error.message : "快捷下单初始化失败");
      } finally {
        setLoading(false);
      }
    };
    void resolveSecurity();
  }, [symbol, orderType, limitPrice]);

  const handleSubmit = async (side: "buy" | "sell") => {
    setMessage(null);
    if (!selectedSecurity || !quote) {
      setMessage("当前标的未就绪，请先在交易页中选择可交易标的。");
      return;
    }
    if (quote.restrictions.length > 0 || !quote.tradeable || quote.trade_session !== "regular") {
      setMessage(quote.restrictions[0] ?? "当前不在 provider 支持的常规交易时段。");
      return;
    }
    if (!Number.isFinite(qty) || qty <= 0) {
      setMessage("请输入合法数量");
      return;
    }
    if (selectedSecurity.lot_size > 1 && qty % selectedSecurity.lot_size !== 0) {
      setMessage(`${selectedSecurity.symbol} 每手 ${selectedSecurity.lot_size} 股，请按整手数量下单`);
      return;
    }
    if (orderType === "limit") {
      const submittedPrice = Number(limitPrice);
      if (!Number.isFinite(submittedPrice) || submittedPrice <= 0) {
        setMessage("请输入合法限价");
        return;
      }
    }
    if (side === "buy" && estimate && qty > estimate.cash_max_qty) {
      setMessage("数量超过最大可买");
      return;
    }
    if (side === "sell" && estimate && qty > estimate.sell_max_qty) {
      setMessage("数量超过最大可卖");
      return;
    }

    setSubmitting(true);
    try {
      const result = await submitTradingOrder({
        symbol: selectedSecurity.symbol,
        side,
        order_type: orderType,
        quantity: qty,
        submitted_price: orderType === "limit" ? Number(limitPrice) : undefined,
      });
      setMessage(result.status === "filled" ? "订单已成交，可在交易页查看委托与成交明细。" : "订单已提交。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "下单失败");
    } finally {
      setSubmitting(false);
    }
  };

  const providerReady = Boolean(providerStatus);
  const providerTone =
    providerStatus?.provider === "mock" ? "amber" : providerStatus?.provider === "futu" ? "sky" : "green";
  const providerTitle =
    providerStatus?.provider === "mock"
      ? "Mock fallback"
      : providerStatus?.provider === "futu"
        ? "Futu provider"
        : "Longbridge sandbox 已连接";

  return (
    <div className="relative flex h-1/2 flex-col bg-[#131722] p-4 text-xs">
      <div className="mb-4 flex items-center justify-between shrink-0">
        <span className="text-[10px] font-bold uppercase tracking-wider text-gray-400">
          快捷下单
        </span>
        <div className="flex items-center gap-2">
          {providerReady ? (
            <div
              className={cn(
                "h-1.5 w-1.5 rounded-full",
                providerTone === "green" ? "bg-green-500" : providerTone === "sky" ? "bg-sky-400" : "bg-amber-400",
              )}
              title={providerTitle}
            />
          ) : (
            <div className="h-1.5 w-1.5 animate-pulse rounded-full bg-yellow-500" title="初始化中…" />
          )}
          <MoreHorizontal size={14} className="cursor-pointer text-gray-500" />
        </div>
      </div>

      {loading ? (
        <div className="flex flex-1 items-center justify-center gap-2 text-gray-500">
          <LoaderCircle className="h-4 w-4 animate-spin" />
          正在初始化…
        </div>
      ) : (
        <>
          <div className="flex-1 space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-gray-500">交易标的</span>
              <div className="text-right">
                <div className="font-mono font-bold text-white">
                  {selectedSecurity?.symbol ?? "未映射"}
                </div>
                <div className="text-[10px] text-gray-500">{selectedSecurity?.name ?? symbol}</div>
              </div>
            </div>

            {quote ? (
              <div className="flex items-center justify-between">
                <span className="text-gray-500">最新价</span>
                <span className={cn("font-mono font-bold", quote.change >= 0 ? "text-emerald-400" : "text-red-400")}>
                  ${quote.last_price.toFixed(2)}
                </span>
              </div>
            ) : null}

            <div className="flex items-center justify-between">
              <span className="text-gray-500">订单类型</span>
              <button
                className="flex w-28 items-center justify-between gap-1 rounded border border-gray-700 bg-[#2a2e39] px-2 py-1 text-white transition-colors hover:text-blue-400"
                onClick={() => setOrderType(orderType === "market" ? "limit" : "market")}
              >
                <span>{orderType === "market" ? "市价" : "限价"}</span>
                <ChevronDown size={12} />
              </button>
            </div>

            {orderType === "limit" ? (
              <div className="flex items-center justify-between">
                <span className="text-gray-500">委托价</span>
                <div className="w-24 rounded border border-gray-700 bg-[#2a2e39] px-2 py-1 text-right">
                  <input
                    type="text"
                    placeholder="价格"
                    value={limitPrice}
                    onChange={(event) => setLimitPrice(event.target.value)}
                    className="w-full bg-transparent text-right font-mono text-xs text-white outline-none"
                  />
                </div>
              </div>
            ) : null}

            <div className="flex items-center justify-between">
              <span className="text-gray-500">数量</span>
              <div className="w-24 rounded border border-gray-700 bg-[#2a2e39] px-2 py-1 text-right">
                <input
                  type="number"
                  min={1}
                  value={qty}
                  onChange={(event) => setQty(Math.max(1, Number.parseInt(event.target.value, 10) || 1))}
                  className="w-full bg-transparent text-right font-mono text-xs text-white outline-none"
                />
              </div>
            </div>

            {estimate ? (
              <div className="rounded border border-gray-800 bg-[#171b24] px-3 py-2 text-[10px] text-gray-400">
                <div className="flex justify-between">
                  <span>最大可买</span>
                  <span>{estimate.cash_max_qty} 股</span>
                </div>
                <div className="mt-1 flex justify-between">
                  <span>最大可卖</span>
                  <span>{estimate.sell_max_qty} 股</span>
                </div>
              </div>
            ) : null}

            {providerStatus?.provider === "mock" ? (
              <div className="rounded border border-amber-500/20 bg-amber-500/10 px-3 py-2 text-[10px] text-amber-300">
                当前为 mock fallback。填入 Longbridge 配置后，主交易页与快捷下单会自动切换到 broker sandbox。
              </div>
            ) : providerStatus?.provider === "futu" ? (
              <div className="rounded border border-sky-500/20 bg-sky-500/10 px-3 py-2 text-[10px] text-sky-300">
                当前为 Futu provider。若 SDK / OpenD / 配置未就绪，接口会返回结构化不可用错误。
              </div>
            ) : null}

            {message ? (
              <div className="rounded border border-blue-500/20 bg-blue-500/10 px-3 py-2 text-[10px] text-blue-300">
                {message}
              </div>
            ) : null}
          </div>

          <div className="mt-auto flex gap-2 border-t border-gray-800 pt-3 shrink-0">
            <button
              className={cn(
                "flex-1 rounded border py-2 text-[11px] font-bold uppercase transition-colors",
                submitting
                  ? "cursor-default border-emerald-600/20 bg-emerald-600/10 text-emerald-500/60"
                  : "border-emerald-600/30 bg-emerald-600/20 text-emerald-500 hover:bg-emerald-600/30",
              )}
              onClick={() => void handleSubmit("buy")}
              disabled={submitting}
            >
              {submitting ? <LoaderCircle className="mx-auto h-4 w-4 animate-spin" /> : "买入"}
            </button>
            <button
              className={cn(
                "flex-1 rounded border py-2 text-[11px] font-bold uppercase transition-colors",
                submitting
                  ? "cursor-default border-red-600/20 bg-red-600/10 text-red-500/60"
                  : "border-red-600/30 bg-red-600/20 text-red-500 hover:bg-red-600/30",
              )}
              onClick={() => void handleSubmit("sell")}
              disabled={submitting}
            >
              {submitting ? <RefreshCw className="mx-auto h-4 w-4 animate-spin" /> : "卖出"}
            </button>
          </div>
        </>
      )}
      <FeatureGuideButton guideKey="market.chart.order" className="bottom-3 right-3" />
    </div>
  );
}

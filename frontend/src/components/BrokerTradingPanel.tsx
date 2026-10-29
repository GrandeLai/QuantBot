/**
 * 交易执行面板 — broker sandbox/testnet 优先，mock provider 兜底。
 *
 * 说明：
 * - 当前 UI 不依赖本地撮合 session / 自定义撮合引擎
 * - 页面所有交易、资产、持仓、委托、成交、资金流水均通过统一 /api/trading 链路获取
 * - 若 broker 未配置或不可用，后端会显式回退到 mock provider，并在页面上展示提示
 */
import { startTransition, useDeferredValue, useEffect, useState } from "react";
import {
  Activity,
  ArrowDownRight,
  ArrowUpRight,
  BadgeDollarSign,
  Banknote,
  Briefcase,
  DatabaseZap,
  LoaderCircle,
  RefreshCw,
  Search,
  ShieldAlert,
  ShieldCheck,
  ShoppingCart,
  TrendingUp,
  Wallet,
} from "lucide-react";
import FeatureGuideButton from "@/components/guides/FeatureGuideButton";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  cancelTradingOrder,
  estimateTradingOrder,
  fetchCashFlows,
  fetchHistoryExecutions,
  fetchHistoryOrders,
  fetchOrderDetail,
  fetchOrderEvents,
  fetchOrderReport,
  fetchTradingRiskStatus,
  fetchTodayExecutions,
  fetchTodayOrders,
  fetchTradingAccount,
  fetchTradingPositions,
  fetchTradingQuotes,
  fetchTradingStatus,
  searchTradingSecurities,
  submitTradingOrder,
  type PagedResponse,
  type TradingAccountOverview,
  type TradingCancelResult,
  type TradingCashFlow,
  type TradingExecution,
  type TradingExecutionReport,
  type TradingOrder,
  type TradingOrderEvent,
  type TradingOrderEstimate,
  type TradingOrderStatus,
  type TradingOrderType,
  type TradingPosition,
  type TradingProviderStatus,
  type TradingQuote,
  type TradingRiskStatus,
  type TradingSecurity,
  type TradingSubmitResult,
} from "@/api/trading";
import { cn } from "@/lib/utils";
import { useTradingStore } from "@/store/tradingStore";

type MainTab = "positions" | "orders" | "executions" | "cashflows";
type HistoryTab = "today" | "history";
type FeedbackTone = "success" | "error" | "info";

interface FeedbackState {
  tone: FeedbackTone;
  message: string;
}

interface ConfirmState {
  open: boolean;
  submitting: boolean;
}

function formatMoney(value: number, currency = "USD") {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
    maximumFractionDigits: currency === "HKD" ? 2 : 2,
  }).format(value);
}

function formatPct(value: number) {
  return `${value >= 0 ? "+" : ""}${(value * 100).toFixed(2)}%`;
}

function formatSignedMoney(value: number, currency = "USD") {
  return `${value >= 0 ? "+" : ""}${formatMoney(value, currency)}`;
}

function sessionLabel(session: TradingQuote["trade_session"]) {
  switch (session) {
    case "regular":
      return "常规时段";
    case "pre_market":
      return "盘前";
    case "post_market":
      return "盘后";
    case "midday_break":
      return "午间休市";
    case "closed":
      return "休市";
    default:
      return "未知";
  }
}

function marketLabel(security: { market: string; asset_type: string }) {
  const market = security.market === "HK" ? "港股" : security.market === "US" ? "美股" : "未知市场";
  const asset =
    security.asset_type === "etf"
      ? "ETF"
      : security.asset_type === "warrant"
        ? "轮证"
        : security.asset_type === "stock"
          ? "股票"
          : security.asset_type === "option"
            ? "期权"
            : security.asset_type === "otc"
              ? "OTC"
              : "资产";
  return `${market} · ${asset}`;
}

function orderStatusLabel(status: TradingOrderStatus) {
  switch (status) {
    case "pending_submit":
      return "待提交";
    case "submitted":
      return "已提交";
    case "partial_filled":
      return "部分成交";
    case "filled":
      return "已成交";
    case "canceled":
      return "已撤销";
    case "rejected":
      return "已拒绝";
    case "expired":
      return "已失效";
    default:
      return "未知";
  }
}

function statusBadgeClass(status: TradingOrderStatus) {
  switch (status) {
    case "filled":
      return "bg-emerald-500/10 text-emerald-400 border-emerald-500/20";
    case "submitted":
    case "pending_submit":
      return "bg-blue-500/10 text-blue-400 border-blue-500/20";
    case "partial_filled":
      return "bg-amber-500/10 text-amber-400 border-amber-500/20";
    case "canceled":
    case "expired":
      return "bg-zinc-500/10 text-zinc-300 border-zinc-500/20";
    case "rejected":
      return "bg-red-500/10 text-red-400 border-red-500/20";
    default:
      return "bg-zinc-500/10 text-zinc-300 border-zinc-500/20";
  }
}

function FeedbackBanner({ feedback }: { feedback: FeedbackState | null }) {
  if (!feedback) return null;

  return (
    <div
      className={cn(
        "rounded-2xl border px-4 py-3 text-sm",
        feedback.tone === "success" && "border-emerald-500/20 bg-emerald-500/10 text-emerald-300",
        feedback.tone === "error" && "border-red-500/20 bg-red-500/10 text-red-300",
        feedback.tone === "info" && "border-blue-500/20 bg-blue-500/10 text-blue-300",
      )}
    >
      {feedback.message}
    </div>
  );
}

function MetricCard({
  label,
  value,
  sub,
  tone = "default",
  icon: Icon,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "default" | "positive" | "negative";
  icon: React.ElementType;
}) {
  return (
    <Card className="border-border bg-card/40 backdrop-blur-sm">
      <CardContent className="p-5">
        <div className="mb-4 flex items-center justify-between">
          <span className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
            {label}
          </span>
          <div className="rounded-xl bg-background/60 p-2 text-muted-foreground">
            <Icon className="h-4 w-4" />
          </div>
        </div>
        <div
          className={cn(
            "text-2xl font-bold tracking-tight",
            tone === "positive" && "text-emerald-400",
            tone === "negative" && "text-red-400",
          )}
        >
          {value}
        </div>
        {sub ? <div className="mt-1 text-xs text-muted-foreground">{sub}</div> : null}
      </CardContent>
    </Card>
  );
}

function ProviderBanner({ status }: { status: TradingProviderStatus | null }) {
  if (!status) return null;

  const isMock = status.provider === "mock";
  const isFutu = status.provider === "futu";
  return (
    <Card
      className={cn(
        "border backdrop-blur-sm",
        isMock
          ? "border-amber-500/20 bg-amber-500/10"
          : isFutu
            ? "border-sky-500/20 bg-sky-500/10"
            : "border-emerald-500/20 bg-emerald-500/10",
      )}
    >
      <CardContent className="flex flex-col gap-3 p-5">
        <div className="flex items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            {isMock ? (
              <ShieldAlert className="h-5 w-5 text-amber-400" />
            ) : isFutu ? (
              <ShieldAlert className="h-5 w-5 text-sky-400" />
            ) : (
              <ShieldCheck className="h-5 w-5 text-emerald-400" />
            )}
            <div>
              <div className="text-sm font-semibold text-foreground">
                {isMock
                  ? "当前使用 Mock Fallback"
                  : isFutu
                    ? "当前接入 Futu Provider"
                    : "当前接入 Longbridge Sandbox"}
              </div>
              <div className="text-xs text-muted-foreground">
                {status.reason ?? "交易、资产、持仓、委托、成交均通过统一交易 provider 链路提供。"}
              </div>
            </div>
          </div>
          <Badge
            variant="outline"
            className={cn(
              "border-current/20 px-2 py-1 uppercase",
              isMock ? "text-amber-300" : isFutu ? "text-sky-300" : "text-emerald-300",
            )}
          >
            {status.provider} · {status.mode}
          </Badge>
        </div>
        <div className="flex flex-wrap gap-2">
          <Badge variant="outline" className="border-border/60 bg-background/40">
            支持港股 / 美股股票 / ETF / 港股轮证
          </Badge>
          {isFutu ? (
            <Badge variant="outline" className="border-sky-500/20 bg-sky-500/10 text-sky-300">
              当前阶段先完成 Futu provider 接入与状态暴露，完整交易能力后续补齐
            </Badge>
          ) : (
            <>
              <Badge variant="outline" className="border-border/60 bg-background/40">
                美股做空官方支持，当前 UI 暂未开放
              </Badge>
              <Badge variant="outline" className="border-red-500/20 bg-red-500/10 text-red-300">
                不支持 OTC / 盘前盘后 / 期权
              </Badge>
            </>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function SearchDropdown({
  loading,
  query,
  items,
  onSelect,
}: {
  loading: boolean;
  query: string;
  items: TradingSecurity[];
  onSelect: (security: TradingSecurity) => void;
}) {
  if (!query.trim()) return null;
  return (
    <div className="absolute left-0 right-0 top-[calc(100%+0.5rem)] z-20 overflow-hidden rounded-2xl border border-border bg-popover shadow-2xl">
      <ScrollArea className="max-h-72">
        {loading ? (
          <div className="px-4 py-5 text-sm text-muted-foreground">搜索中…</div>
        ) : items.length === 0 ? (
          <div className="px-4 py-5 text-sm text-muted-foreground">
            没有找到匹配标的。当前优先支持已接入 unified trading provider 的港美股票 / ETF。
          </div>
        ) : (
          items.map((item) => (
            <button
              key={item.symbol}
              onClick={() => onSelect(item)}
              className="flex w-full items-start justify-between gap-3 border-b border-border/50 px-4 py-3 text-left transition-colors hover:bg-accent/60"
            >
              <div className="min-w-0">
                <div className="font-medium text-foreground">{item.name}</div>
                <div className="text-xs text-muted-foreground">
                  {item.symbol} · {marketLabel(item)}
                </div>
              </div>
              <Badge variant="outline" className="shrink-0 border-border/60 bg-background/40">
                {item.market}
              </Badge>
            </button>
          ))
        )}
      </ScrollArea>
    </div>
  );
}

function PaginationBar({
  page,
  pageSize,
  total,
  onPrev,
  onNext,
}: {
  page: number;
  pageSize: number;
  total: number;
  onPrev: () => void;
  onNext: () => void;
}) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  return (
    <div className="mt-4 flex items-center justify-between text-xs text-muted-foreground">
      <span>
        第 {page} / {totalPages} 页，共 {total} 条
      </span>
      <div className="flex items-center gap-2">
        <Button variant="outline" size="sm" onClick={onPrev} disabled={page <= 1}>
          上一页
        </Button>
        <Button variant="outline" size="sm" onClick={onNext} disabled={page >= totalPages}>
          下一页
        </Button>
      </div>
    </div>
  );
}

function ConfirmDialog({
  open,
  onClose,
  onConfirm,
  submitting,
  summary,
}: {
  open: boolean;
  onClose: () => void;
  onConfirm: () => void;
  submitting: boolean;
  summary: React.ReactNode;
}) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4 backdrop-blur-sm">
      <div className="w-full max-w-lg overflow-hidden rounded-3xl border border-border bg-card shadow-2xl">
        <div className="border-b border-border px-6 py-4">
          <div className="text-lg font-semibold text-foreground">确认下单</div>
          <div className="mt-1 text-sm text-muted-foreground">
            请再次确认订单信息。当前版本仅开放已完成接线的标准市价 / 限价单。
          </div>
        </div>
        <div className="px-6 py-5">{summary}</div>
        <div className="flex items-center justify-end gap-3 border-t border-border px-6 py-4">
          <Button variant="outline" onClick={onClose} disabled={submitting}>
            取消
          </Button>
          <Button onClick={onConfirm} disabled={submitting}>
            {submitting ? <LoaderCircle className="mr-2 h-4 w-4 animate-spin" /> : null}
            提交订单
          </Button>
        </div>
      </div>
    </div>
  );
}

function LoadingState({ text }: { text: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-16 text-sm text-muted-foreground">
      <LoaderCircle className="h-4 w-4 animate-spin" />
      {text}
    </div>
  );
}

function EmptyState({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-16 text-center">
      <DatabaseZap className="h-10 w-10 text-muted-foreground/30" />
      <div className="text-sm font-medium text-foreground">{title}</div>
      <div className="max-w-md text-xs leading-relaxed text-muted-foreground">{detail}</div>
    </div>
  );
}

export default function BrokerTradingPanel() {
  const {
    query,
    selectedSecurity,
    quote,
    draft,
    setQuery,
    setSelectedSecurity,
    setQuote,
    patchDraft,
    quickSell,
  } = useTradingStore();

  const deferredQuery = useDeferredValue(query);

  const [status, setStatus] = useState<TradingProviderStatus | null>(null);
  const [riskStatus, setRiskStatus] = useState<TradingRiskStatus | null>(null);
  const [account, setAccount] = useState<TradingAccountOverview | null>(null);
  const [positions, setPositions] = useState<TradingPosition[]>([]);
  const [todayOrders, setTodayOrders] = useState<PagedResponse<TradingOrder> | null>(null);
  const [historyOrders, setHistoryOrders] = useState<PagedResponse<TradingOrder> | null>(null);
  const [todayExecutions, setTodayExecutions] = useState<PagedResponse<TradingExecution> | null>(null);
  const [historyExecutions, setHistoryExecutions] = useState<PagedResponse<TradingExecution> | null>(null);
  const [cashFlows, setCashFlows] = useState<PagedResponse<TradingCashFlow> | null>(null);
  const [estimate, setEstimate] = useState<TradingOrderEstimate | null>(null);
  const [searchResults, setSearchResults] = useState<TradingSecurity[]>([]);
  const [selectedOrder, setSelectedOrder] = useState<TradingOrder | null>(null);
  const [selectedOrderEvents, setSelectedOrderEvents] = useState<TradingOrderEvent[]>([]);
  const [selectedOrderReport, setSelectedOrderReport] = useState<TradingExecutionReport | null>(null);

  const [mainTab, setMainTab] = useState<MainTab>("positions");
  const [orderTab, setOrderTab] = useState<HistoryTab>("today");
  const [executionTab, setExecutionTab] = useState<HistoryTab>("today");

  const [loadingDashboard, setLoadingDashboard] = useState(true);
  const [loadingSearch, setLoadingSearch] = useState(false);
  const [loadingEstimate, setLoadingEstimate] = useState(false);
  const [submittingOrder, setSubmittingOrder] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [feedback, setFeedback] = useState<FeedbackState | null>(null);
  const [confirm, setConfirm] = useState<ConfirmState>({ open: false, submitting: false });
  const [pageState, setPageState] = useState({
    todayOrders: 1,
    historyOrders: 1,
    todayExecutions: 1,
    historyExecutions: 1,
    cashFlows: 1,
  });

  const [sectionError, setSectionError] = useState<Record<string, string | null>>({
    dashboard: null,
    orderBook: null,
    executions: null,
    cashFlows: null,
  });
  const detailGuideKey =
    mainTab === "positions"
      ? "trading.execution.positions"
      : mainTab === "orders"
        ? (orderTab === "today" ? "trading.execution.orders.today" : "trading.execution.orders.history")
        : mainTab === "executions"
          ? (executionTab === "today"
            ? "trading.execution.executions.today"
            : "trading.execution.executions.history")
          : "trading.execution.cashflows";

  const refreshOrders = async (todayPage = pageState.todayOrders, historyPage = pageState.historyOrders) => {
    try {
      const [today, history] = await Promise.all([
        fetchTodayOrders(todayPage, 10),
        fetchHistoryOrders(historyPage, 10),
      ]);
      setTodayOrders(today);
      setHistoryOrders(history);
      setSectionError((prev) => ({ ...prev, orderBook: null }));
    } catch (error) {
      setSectionError((prev) => ({
        ...prev,
        orderBook: error instanceof Error ? error.message : "委托查询失败",
      }));
    }
  };

  const refreshExecutions = async (
    todayPage = pageState.todayExecutions,
    historyPage = pageState.historyExecutions,
  ) => {
    try {
      const [today, history] = await Promise.all([
        fetchTodayExecutions(todayPage, 10),
        fetchHistoryExecutions(historyPage, 10),
      ]);
      setTodayExecutions(today);
      setHistoryExecutions(history);
      setSectionError((prev) => ({ ...prev, executions: null }));
    } catch (error) {
      setSectionError((prev) => ({
        ...prev,
        executions: error instanceof Error ? error.message : "成交查询失败",
      }));
    }
  };

  const refreshCashFlows = async (page = pageState.cashFlows) => {
    try {
      const result = await fetchCashFlows(page, 10);
      setCashFlows(result);
      setSectionError((prev) => ({ ...prev, cashFlows: null }));
    } catch (error) {
      setSectionError((prev) => ({
        ...prev,
        cashFlows: error instanceof Error ? error.message : "资金流水查询失败",
      }));
    }
  };

  const refreshDashboard = async () => {
    setLoadingDashboard(true);
    try {
      const [providerStatus, nextRiskStatus, accountOverview, positionItems] = await Promise.all([
        fetchTradingStatus(),
        fetchTradingRiskStatus(),
        fetchTradingAccount(),
        fetchTradingPositions(),
      ]);
      setStatus(providerStatus);
      setRiskStatus(nextRiskStatus);
      setAccount(accountOverview);
      setPositions(positionItems);
      setSectionError((prev) => ({ ...prev, dashboard: null }));
    } catch (error) {
      setSectionError((prev) => ({
        ...prev,
        dashboard: error instanceof Error ? error.message : "账户总览加载失败",
      }));
    } finally {
      setLoadingDashboard(false);
    }
  };

  const refreshQuote = async (security: TradingSecurity | null) => {
    if (!security) {
      setQuote(null);
      setEstimate(null);
      return;
    }
    try {
      const [quoteItem] = await fetchTradingQuotes([security.symbol]);
      setQuote(quoteItem ?? null);
      if (draft.orderType === "limit" && !draft.submittedPrice && quoteItem) {
        patchDraft({ submittedPrice: quoteItem.last_price.toFixed(2) });
      }
    } catch (error) {
      setFeedback({
        tone: "error",
        message: error instanceof Error ? error.message : "行情拉取失败",
      });
    }
  };

  const refreshAll = async () => {
    setRefreshing(true);
    await Promise.all([
      refreshDashboard(),
      refreshOrders(),
      refreshExecutions(),
      refreshCashFlows(),
      refreshQuote(selectedSecurity),
    ]);
    setRefreshing(false);
  };

  useEffect(() => {
    void refreshAll();
    const timer = window.setInterval(() => {
      startTransition(() => {
        void refreshAll();
      });
    }, 15000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const run = async () => {
      if (!deferredQuery.trim()) {
        setSearchResults([]);
        return;
      }
      setLoadingSearch(true);
      try {
        const results = await searchTradingSecurities(deferredQuery);
        setSearchResults(results);
      } catch (error) {
        setFeedback({
          tone: "error",
          message: error instanceof Error ? error.message : "标的搜索失败",
        });
      } finally {
        setLoadingSearch(false);
      }
    };
    void run();
  }, [deferredQuery]);

  useEffect(() => {
    void refreshQuote(selectedSecurity);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedSecurity]);

  useEffect(() => {
    const run = async () => {
      if (!selectedSecurity) {
        setEstimate(null);
        return;
      }
      setLoadingEstimate(true);
      try {
        const result = await estimateTradingOrder({
          symbol: selectedSecurity.symbol,
          side: draft.side,
          order_type: draft.orderType,
          submitted_price:
            draft.orderType === "limit" && draft.submittedPrice
              ? Number(draft.submittedPrice)
              : undefined,
        });
        setEstimate(result);
      } catch (error) {
        setEstimate(null);
        setFeedback({
          tone: "error",
          message: error instanceof Error ? error.message : "下单预估失败",
        });
      } finally {
        setLoadingEstimate(false);
      }
    };
    void run();
  }, [draft.orderType, draft.side, draft.submittedPrice, selectedSecurity]);

  useEffect(() => {
    void refreshOrders(pageState.todayOrders, pageState.historyOrders);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pageState.todayOrders, pageState.historyOrders]);

  useEffect(() => {
    void refreshExecutions(pageState.todayExecutions, pageState.historyExecutions);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pageState.todayExecutions, pageState.historyExecutions]);

  useEffect(() => {
    void refreshCashFlows(pageState.cashFlows);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pageState.cashFlows]);

  const selectedOrderList = orderTab === "today" ? todayOrders : historyOrders;
  const selectedExecutionList = executionTab === "today" ? todayExecutions : historyExecutions;

  const validateOrder = (): string | null => {
    if (!selectedSecurity) return "请先通过代码或名称选择交易标的";
    if (!quote) return "当前行情尚未加载完成，请稍后再试";
    if (quote.restrictions.length > 0 || !quote.tradeable) {
      return quote.restrictions[0] ?? "当前标的不在 broker sandbox 支持范围内";
    }
    if (quote.trade_session !== "regular") {
      return `当前处于 ${sessionLabel(quote.trade_session)}，broker sandbox 仅支持常规交易时段`;
    }

    const quantity = Number.parseInt(draft.quantity, 10);
    if (!Number.isFinite(quantity) || quantity <= 0) return "请输入合法的委托数量";
    if (selectedSecurity.lot_size > 1 && quantity % selectedSecurity.lot_size !== 0) {
      return `${selectedSecurity.symbol} 每手 ${selectedSecurity.lot_size} 股，请按整手数量下单`;
    }

    if (draft.orderType === "limit") {
      const submittedPrice = Number(draft.submittedPrice);
      if (!Number.isFinite(submittedPrice) || submittedPrice <= 0) {
        return "限价单必须填写合法的委托价格";
      }
    }

    if (draft.side === "buy" && estimate && quantity > estimate.cash_max_qty) {
      return "委托数量超过最大可买数量";
    }
    if (draft.side === "sell" && estimate && quantity > estimate.sell_max_qty) {
      return "委托数量超过最大可卖数量";
    }
    return null;
  };

  const openConfirm = () => {
    const message = validateOrder();
    if (message) {
      setFeedback({ tone: "error", message });
      return;
    }
    setConfirm({ open: true, submitting: false });
  };

  const performSubmit = async () => {
    if (!selectedSecurity) return;
    setConfirm((prev) => ({ ...prev, submitting: true }));
    setSubmittingOrder(true);
    try {
      const quantity = Number.parseInt(draft.quantity, 10);
      const result: TradingSubmitResult = await submitTradingOrder({
        symbol: selectedSecurity.symbol,
        side: draft.side,
        order_type: draft.orderType,
        quantity,
        submitted_price:
          draft.orderType === "limit" && draft.submittedPrice
            ? Number(draft.submittedPrice)
            : undefined,
      });
      setFeedback({
        tone: "success",
        message:
          result.status === "filled"
            ? `订单已成交：${selectedSecurity.symbol} ${draft.side === "buy" ? "买入" : "卖出"} ${quantity}`
            : `订单已提交：${result.order_id}`,
      });
      setConfirm({ open: false, submitting: false });
      startTransition(() => {
        void refreshAll();
      });
    } catch (error) {
      setFeedback({
        tone: "error",
        message: error instanceof Error ? error.message : "下单失败",
      });
      setConfirm((prev) => ({ ...prev, submitting: false }));
    } finally {
      setSubmittingOrder(false);
    }
  };

  const handleCancelOrder = async (orderId: string) => {
    try {
      const result: TradingCancelResult = await cancelTradingOrder(orderId);
      setFeedback({ tone: "success", message: result.message });
      startTransition(() => {
        void refreshOrders();
      });
      if (selectedOrder?.order_id === orderId) {
        const detail = await fetchOrderDetail(orderId);
        setSelectedOrder(detail);
        setSelectedOrderEvents(await fetchOrderEvents(orderId));
        setSelectedOrderReport(await fetchOrderReport(orderId));
      }
    } catch (error) {
      setFeedback({
        tone: "error",
        message: error instanceof Error ? error.message : "撤单失败",
      });
    }
  };

  const handleSelectOrder = async (orderId: string) => {
    try {
      const detail = await fetchOrderDetail(orderId);
      setSelectedOrder(detail);
      setSelectedOrderEvents(await fetchOrderEvents(orderId));
      setSelectedOrderReport(await fetchOrderReport(orderId));
    } catch (error) {
      setFeedback({
        tone: "error",
        message: error instanceof Error ? error.message : "订单详情加载失败",
      });
      setSelectedOrderEvents([]);
      setSelectedOrderReport(null);
    }
  };

  const orderSummary = (
    <div className="grid gap-3 text-sm">
      <div className="grid grid-cols-2 gap-3">
        <SummaryItem label="标的" value={selectedSecurity?.symbol ?? "—"} />
        <SummaryItem label="名称" value={selectedSecurity?.name ?? "—"} />
        <SummaryItem label="方向" value={draft.side === "buy" ? "买入" : "卖出"} />
        <SummaryItem label="订单类型" value={draft.orderType === "market" ? "市价" : "限价"} />
        <SummaryItem label="数量" value={draft.quantity || "—"} />
        <SummaryItem
          label="参考价格"
          value={
            draft.orderType === "limit"
              ? draft.submittedPrice || "—"
              : quote
                ? quote.last_price.toFixed(2)
                : "—"
          }
        />
      </div>
      {estimate ? (
        <div className="rounded-2xl border border-border bg-background/50 p-3 text-xs text-muted-foreground">
          {draft.side === "buy"
            ? `最大可买：${estimate.cash_max_qty} 股`
            : `最大可卖：${estimate.sell_max_qty} 股`}
        </div>
      ) : null}
    </div>
  );

  return (
    <div className="relative space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-3">
            <h2 className="text-2xl font-bold tracking-tight">交易执行</h2>
            <Badge variant="outline" className="border-border/60 bg-card/60">
              Longbridge First
            </Badge>
          </div>
          <p className="mt-2 max-w-3xl text-sm leading-relaxed text-muted-foreground">
            交易、资产、持仓、委托、成交与资金流水统一走 broker 执行链路。
            若当前未配置 broker 凭证，系统会自动回退到结构一致的 mock provider 以便本地验证 UI 闭环。
          </p>
        </div>
        <Button variant="outline" onClick={() => void refreshAll()} disabled={refreshing}>
          {refreshing ? <LoaderCircle className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-2 h-4 w-4" />}
          刷新
        </Button>
      </div>

      <ProviderBanner status={status} />
      <FeedbackBanner feedback={feedback} />

      {loadingDashboard ? (
        <LoadingState text="正在加载交易账户总览…" />
      ) : sectionError.dashboard ? (
        <Card className="border-red-500/20 bg-red-500/10">
          <CardContent className="px-5 py-4 text-sm text-red-300">{sectionError.dashboard}</CardContent>
        </Card>
      ) : (
        <>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
            <MetricCard
              label="总资产"
              value={formatMoney(account?.total_assets ?? 0, account?.currency ?? "USD")}
              icon={Wallet}
            />
            <MetricCard
              label="可用资金"
              value={formatMoney(account?.available_cash ?? 0, account?.currency ?? "USD")}
              icon={Banknote}
            />
            <MetricCard
              label="持仓市值"
              value={formatMoney(account?.positions_market_value ?? 0, account?.currency ?? "USD")}
              icon={Briefcase}
            />
            <MetricCard
              label="今日盈亏"
              value={formatSignedMoney(account?.today_pnl ?? 0, account?.currency ?? "USD")}
              sub={formatPct(account?.today_pnl_pct ?? 0)}
              tone={(account?.today_pnl ?? 0) >= 0 ? "positive" : "negative"}
              icon={Activity}
            />
            <MetricCard
              label="总盈亏"
              value={formatSignedMoney(account?.total_pnl ?? 0, account?.currency ?? "USD")}
              sub={formatPct(account?.total_pnl_pct ?? 0)}
              tone={(account?.total_pnl ?? 0) >= 0 ? "positive" : "negative"}
              icon={TrendingUp}
            />
            <MetricCard
              label="购买力"
              value={formatMoney(account?.buying_power ?? 0, account?.currency ?? "USD")}
              icon={BadgeDollarSign}
            />
          </div>

          {riskStatus ? (
            <Card className={cn("border", riskStatus.halted ? "border-red-500/20 bg-red-500/10" : "border-border bg-card/40")}>
              <CardHeader className="pb-3">
                <CardTitle className="text-lg font-semibold">交易风控</CardTitle>
              </CardHeader>
              <CardContent className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                <SummaryItem label="状态" value={riskStatus.halted ? "已熔断" : "正常"} />
                <SummaryItem label="最大持仓数" value={`${riskStatus.max_position_count}`} mono />
                <SummaryItem
                  label="单标的上限"
                  value={`${(riskStatus.max_single_position_pct * 100).toFixed(1)}%`}
                  mono
                />
                <SummaryItem
                  label="日内亏损阈值"
                  value={
                    riskStatus.daily_loss_limit_pct != null
                      ? `${(riskStatus.daily_loss_limit_pct * 100).toFixed(1)}%`
                      : "—"
                  }
                  mono
                />
                <SummaryItem
                  label="单笔金额上限"
                  value={riskStatus.max_order_value != null ? `${riskStatus.max_order_value.toFixed(0)}` : "—"}
                  mono
                />
                <SummaryItem label="持仓数量" value={`${riskStatus.open_position_count}`} mono />
                <SummaryItem label="剩余仓位槽" value={`${riskStatus.available_position_slots}`} mono />
                <SummaryItem
                  label="最大集中度"
                  value={
                    riskStatus.largest_position_symbol
                      ? `${riskStatus.largest_position_symbol} · ${(riskStatus.largest_position_ratio * 100).toFixed(1)}%`
                      : "当前无持仓"
                  }
                />
                <SummaryItem
                  label="当前日内盈亏"
                  value={`${(riskStatus.current_today_pnl_pct * 100).toFixed(2)}%`}
                  mono
                  valueClass={riskStatus.current_today_pnl_pct >= 0 ? "text-emerald-400" : "text-red-400"}
                />
                <SummaryItem
                  label="风险提示"
                  value={riskStatus.warnings[0] ?? "当前未触发额外风险提示"}
                />
              </CardContent>
            </Card>
          ) : null}

          <div className="grid gap-6 xl:grid-cols-[380px_minmax(0,1fr)]">
            <Card className="border-border bg-card/40">
              <CardHeader className="space-y-1 pb-4">
                <CardTitle className="text-lg font-semibold">交易下单</CardTitle>
                <p className="text-xs leading-relaxed text-muted-foreground">
                  当前仅开放 broker sandbox 已覆盖并在本项目完成接线的标准市价 / 限价单。
                  对 OTC、盘前盘后、期权等官方不支持场景会在产品层直接限制。
                </p>
              </CardHeader>
              <CardContent className="space-y-5">
                <div className="relative space-y-2">
                  <label className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                    标的搜索
                  </label>
                  <div className="relative">
                    <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                    <Input
                      value={query}
                      onChange={(event) => setQuery(event.target.value)}
                      placeholder="输入代码或名称，例如 AAPL、腾讯、SPY"
                      className="pl-10"
                    />
                  </div>
                  <SearchDropdown
                    loading={loadingSearch}
                    query={query}
                    items={searchResults}
                    onSelect={(security) => {
                      setSelectedSecurity(security);
                      setQuery(security.symbol);
                      setSearchResults([]);
                      setFeedback(null);
                    }}
                  />
                </div>

                <div className="rounded-2xl border border-border bg-background/40 p-4">
                  {selectedSecurity ? (
                    <div className="space-y-4">
                      <div className="flex items-start justify-between gap-3">
                        <div>
                          <div className="text-sm font-semibold text-foreground">{selectedSecurity.name}</div>
                          <div className="mt-1 text-xs text-muted-foreground">
                            {selectedSecurity.symbol} · {marketLabel(selectedSecurity)}
                          </div>
                        </div>
                        <Badge variant="outline" className="border-border/60 bg-card/60">
                          {selectedSecurity.market}
                        </Badge>
                      </div>

                      {quote ? (
                        <div className="grid grid-cols-2 gap-3">
                          <SummaryItem
                            label="最新价"
                            value={formatMoney(quote.last_price, selectedSecurity.currency)}
                          />
                          <SummaryItem
                            label="涨跌幅"
                            value={formatPct(quote.change_pct)}
                            valueClass={quote.change >= 0 ? "text-emerald-400" : "text-red-400"}
                          />
                          <SummaryItem label="交易时段" value={sessionLabel(quote.trade_session)} />
                          <SummaryItem label="每手" value={`${selectedSecurity.lot_size} 股`} />
                        </div>
                      ) : (
                        <div className="text-xs text-muted-foreground">正在拉取行情…</div>
                      )}

                      {quote?.restrictions.length ? (
                        <div className="rounded-2xl border border-red-500/20 bg-red-500/10 p-3 text-xs text-red-300">
                          {quote.restrictions.join("；")}
                        </div>
                      ) : null}
                    </div>
                  ) : (
                    <EmptyState
                      title="尚未选择交易标的"
                      detail="请通过代码或名称搜索支持的港美股票 / ETF。当前版本不展示 broker sandbox 不支持的交易入口。"
                    />
                  )}
                </div>

                <div className="space-y-4 rounded-2xl border border-border bg-background/40 p-4">
                  <div className="grid grid-cols-2 gap-2">
                    <Button
                      variant={draft.side === "buy" ? "default" : "outline"}
                      className={cn("font-semibold", draft.side === "buy" && "bg-emerald-600 hover:bg-emerald-500")}
                      onClick={() => patchDraft({ side: "buy" })}
                    >
                      <ArrowUpRight className="mr-2 h-4 w-4" />
                      买入
                    </Button>
                    <Button
                      variant={draft.side === "sell" ? "default" : "outline"}
                      className={cn("font-semibold", draft.side === "sell" && "bg-red-600 hover:bg-red-500")}
                      onClick={() => patchDraft({ side: "sell" })}
                    >
                      <ArrowDownRight className="mr-2 h-4 w-4" />
                      卖出
                    </Button>
                  </div>

                  <div className="grid gap-4 sm:grid-cols-2">
                    <div className="space-y-2">
                      <label className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                        订单类型
                      </label>
                      <Select
                        value={draft.orderType}
                        onValueChange={(value) => patchDraft({ orderType: value as TradingOrderType })}
                      >
                        <SelectTrigger className="w-full">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="market">市价单</SelectItem>
                          <SelectItem value="limit">限价单</SelectItem>
                        </SelectContent>
                      </Select>
                    </div>
                    <div className="space-y-2">
                      <label className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                        数量
                      </label>
                      <Input
                        value={draft.quantity}
                        onChange={(event) => patchDraft({ quantity: event.target.value.replace(/[^\d]/g, "") })}
                        placeholder="输入股数"
                      />
                    </div>
                  </div>

                  {draft.orderType === "limit" ? (
                    <div className="space-y-2">
                      <label className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                        委托价格
                      </label>
                      <Input
                        value={draft.submittedPrice}
                        onChange={(event) =>
                          patchDraft({ submittedPrice: event.target.value.replace(/[^0-9.]/g, "") })
                        }
                        placeholder="输入限价"
                      />
                    </div>
                  ) : null}

                  <div className="grid grid-cols-2 gap-3 rounded-2xl border border-border bg-card/60 p-3 text-xs">
                    <SummaryItem
                      label={draft.side === "buy" ? "最大可买" : "最大可卖"}
                      value={
                        loadingEstimate
                          ? "计算中…"
                          : draft.side === "buy"
                            ? `${estimate?.cash_max_qty ?? 0} 股`
                            : `${estimate?.sell_max_qty ?? 0} 股`
                      }
                    />
                    <SummaryItem
                      label="参考金额"
                      value={
                        quote
                          ? formatMoney(
                              (draft.orderType === "limit" && draft.submittedPrice
                                ? Number(draft.submittedPrice)
                                : quote.last_price) * Number(draft.quantity || 0),
                              selectedSecurity?.currency ?? "USD",
                            )
                          : "—"
                      }
                    />
                  </div>

                  {estimate?.reason ? (
                    <div className="rounded-2xl border border-amber-500/20 bg-amber-500/10 p-3 text-xs text-amber-300">
                      {estimate.reason}
                    </div>
                  ) : null}

                  <Button
                    className="h-11 w-full font-bold"
                    onClick={openConfirm}
                    disabled={submittingOrder || !selectedSecurity}
                  >
                    {submittingOrder ? <LoaderCircle className="mr-2 h-4 w-4 animate-spin" /> : <ShoppingCart className="mr-2 h-4 w-4" />}
                    提交订单
                  </Button>
                </div>
              </CardContent>
            </Card>

            <div className="space-y-6">
              <Card className="relative border-border bg-card/40">
                <CardHeader className="pb-3">
                  <div className="flex items-center justify-between gap-4">
                    <CardTitle className="text-lg font-semibold">账户明细</CardTitle>
                    <Tabs value={mainTab} onValueChange={(value) => setMainTab(value as MainTab)} className="w-fit">
                      <TabsList className="bg-card border border-border">
                        <TabsTrigger value="positions">持仓</TabsTrigger>
                        <TabsTrigger value="orders">委托</TabsTrigger>
                        <TabsTrigger value="executions">成交</TabsTrigger>
                        <TabsTrigger value="cashflows">流水</TabsTrigger>
                      </TabsList>
                    </Tabs>
                  </div>
                </CardHeader>
                <CardContent>
                  {mainTab === "positions" ? (
                    positions.length === 0 ? (
                      <EmptyState
                        title="当前没有持仓"
                        detail="买入成功后，这里会显示持仓数量、可卖数量、成本价、最新价与浮动盈亏。"
                      />
                    ) : (
                      <div>
                        <Table>
                          <TableHeader>
                            <TableRow>
                              <TableHead>标的</TableHead>
                              <TableHead className="text-right">持仓 / 可卖</TableHead>
                              <TableHead className="text-right">成本 / 最新</TableHead>
                              <TableHead className="text-right">市值</TableHead>
                              <TableHead className="text-right">浮盈亏</TableHead>
                              <TableHead className="text-right">操作</TableHead>
                            </TableRow>
                          </TableHeader>
                          <TableBody>
                            {positions.map((position) => {
                              const security =
                                searchResults.find((item) => item.symbol === position.symbol) ?? {
                                  symbol: position.symbol,
                                  name: position.name,
                                  market: position.market,
                                  currency: position.currency,
                                  asset_type: position.asset_type,
                                  lot_size: 1,
                                  tradeable: true,
                                  shortable: false,
                                  restrictions: [],
                                };
                              return (
                                <TableRow key={position.symbol}>
                                  <TableCell className="py-3">
                                    <div className="font-medium text-foreground">{position.name}</div>
                                    <div className="text-xs text-muted-foreground">{position.symbol}</div>
                                  </TableCell>
                                  <TableCell className="py-3 text-right font-mono">
                                    <div>{position.quantity}</div>
                                    <div className="text-xs text-muted-foreground">{position.available_quantity}</div>
                                  </TableCell>
                                  <TableCell className="py-3 text-right font-mono">
                                    <div>{position.cost_price.toFixed(2)}</div>
                                    <div className="text-xs text-muted-foreground">{position.last_price.toFixed(2)}</div>
                                  </TableCell>
                                  <TableCell className="py-3 text-right font-mono">
                                    {formatMoney(position.market_value, position.currency)}
                                  </TableCell>
                                  <TableCell
                                    className={cn(
                                      "py-3 text-right font-mono font-semibold",
                                      position.unrealized_pnl >= 0 ? "text-emerald-400" : "text-red-400",
                                    )}
                                  >
                                    <div>{formatSignedMoney(position.unrealized_pnl, position.currency)}</div>
                                    <div className="text-xs">{formatPct(position.unrealized_pnl_pct)}</div>
                                  </TableCell>
                                  <TableCell className="py-3 text-right">
                                    <Button
                                      variant="outline"
                                      size="sm"
                                      onClick={() =>
                                        quickSell({
                                          security,
                                          quantity: position.available_quantity,
                                        })
                                      }
                                    >
                                      快速卖出
                                    </Button>
                                  </TableCell>
                                </TableRow>
                              );
                            })}
                          </TableBody>
                        </Table>
                      </div>
                    )
                  ) : null}

                  {mainTab === "orders" ? (
                    <div>
                      <div className="mb-4 flex items-center justify-between gap-3">
                        <Tabs value={orderTab} onValueChange={(value) => setOrderTab(value as HistoryTab)} className="w-fit">
                          <TabsList className="bg-card border border-border">
                            <TabsTrigger value="today">当前 / 今日委托</TabsTrigger>
                            <TabsTrigger value="history">历史委托</TabsTrigger>
                          </TabsList>
                        </Tabs>
                      </div>
                      {sectionError.orderBook ? (
                        <EmptyState title="委托查询失败" detail={sectionError.orderBook} />
                      ) : !selectedOrderList || selectedOrderList.items.length === 0 ? (
                        <EmptyState
                          title="暂无委托记录"
                          detail="提交订单后，这里会显示当前委托和历史委托，并支持撤单和查看订单详情。"
                        />
                      ) : (
                        <>
                          <Table>
                            <TableHeader>
                              <TableRow>
                                <TableHead>订单</TableHead>
                                <TableHead>方向</TableHead>
                                <TableHead>类型</TableHead>
                                <TableHead className="text-right">数量</TableHead>
                                <TableHead className="text-right">价格</TableHead>
                                <TableHead>状态</TableHead>
                                <TableHead className="text-right">操作</TableHead>
                              </TableRow>
                            </TableHeader>
                            <TableBody>
                              {selectedOrderList.items.map((order) => (
                                <TableRow
                                  key={order.order_id}
                                  className="cursor-pointer"
                                  onClick={() => void handleSelectOrder(order.order_id)}
                                >
                                  <TableCell className="py-3">
                                    <div className="font-medium text-foreground">{order.name}</div>
                                    <div className="text-xs text-muted-foreground">{order.symbol}</div>
                                  </TableCell>
                                  <TableCell className={cn("py-3 font-semibold", order.side === "buy" ? "text-emerald-400" : "text-red-400")}>
                                    {order.side === "buy" ? "买入" : "卖出"}
                                  </TableCell>
                                  <TableCell className="py-3">{order.order_type === "market" ? "市价" : "限价"}</TableCell>
                                  <TableCell className="py-3 text-right font-mono">
                                    {order.executed_quantity}/{order.quantity}
                                  </TableCell>
                                  <TableCell className="py-3 text-right font-mono">
                                    {order.submitted_price ?? order.executed_price ?? "—"}
                                  </TableCell>
                                  <TableCell className="py-3">
                                    <Badge className={cn("border", statusBadgeClass(order.status))}>
                                      {orderStatusLabel(order.status)}
                                    </Badge>
                                  </TableCell>
                                  <TableCell className="py-3 text-right">
                                    {order.status === "submitted" || order.status === "pending_submit" ? (
                                      <Button
                                        variant="outline"
                                        size="sm"
                                        onClick={(event) => {
                                          event.stopPropagation();
                                          void handleCancelOrder(order.order_id);
                                        }}
                                      >
                                        撤单
                                      </Button>
                                    ) : (
                                      <span className="text-xs text-muted-foreground">—</span>
                                    )}
                                  </TableCell>
                                </TableRow>
                              ))}
                            </TableBody>
                          </Table>
                          <PaginationBar
                            page={selectedOrderList.page}
                            pageSize={selectedOrderList.page_size}
                            total={selectedOrderList.total}
                            onPrev={() =>
                              setPageState((prev) => ({
                                ...prev,
                                [orderTab === "today" ? "todayOrders" : "historyOrders"]:
                                  Math.max(1, prev[orderTab === "today" ? "todayOrders" : "historyOrders"] - 1),
                              }))
                            }
                            onNext={() =>
                              setPageState((prev) => ({
                                ...prev,
                                [orderTab === "today" ? "todayOrders" : "historyOrders"]:
                                  prev[orderTab === "today" ? "todayOrders" : "historyOrders"] + 1,
                              }))
                            }
                          />
                        </>
                      )}
                    </div>
                  ) : null}

                  {mainTab === "executions" ? (
                    <div>
                      <div className="mb-4 flex items-center justify-between gap-3">
                        <Tabs value={executionTab} onValueChange={(value) => setExecutionTab(value as HistoryTab)} className="w-fit">
                          <TabsList className="bg-card border border-border">
                            <TabsTrigger value="today">当日成交</TabsTrigger>
                            <TabsTrigger value="history">历史成交</TabsTrigger>
                          </TabsList>
                        </Tabs>
                      </div>
                      {sectionError.executions ? (
                        <EmptyState title="成交查询失败" detail={sectionError.executions} />
                      ) : !selectedExecutionList || selectedExecutionList.items.length === 0 ? (
                        <EmptyState
                          title="暂无成交记录"
                          detail="订单成交后，这里会显示价格、数量、时间和买卖方向。"
                        />
                      ) : (
                        <>
                          <Table>
                            <TableHeader>
                              <TableRow>
                                <TableHead>标的</TableHead>
                                <TableHead>方向</TableHead>
                                <TableHead className="text-right">成交价</TableHead>
                                <TableHead className="text-right">成交量</TableHead>
                                <TableHead>时间</TableHead>
                              </TableRow>
                            </TableHeader>
                            <TableBody>
                              {selectedExecutionList.items.map((execution) => (
                                <TableRow key={execution.execution_id}>
                                  <TableCell className="py-3">
                                    <div className="font-medium text-foreground">{execution.name}</div>
                                    <div className="text-xs text-muted-foreground">{execution.symbol}</div>
                                  </TableCell>
                                  <TableCell className={cn("py-3 font-semibold", execution.side === "buy" ? "text-emerald-400" : "text-red-400")}>
                                    {execution.side === "buy" ? "买入" : "卖出"}
                                  </TableCell>
                                  <TableCell className="py-3 text-right font-mono">
                                    {execution.price.toFixed(2)}
                                  </TableCell>
                                  <TableCell className="py-3 text-right font-mono">
                                    {execution.quantity}
                                  </TableCell>
                                  <TableCell className="py-3 text-xs text-muted-foreground">
                                    {new Date(execution.executed_at).toLocaleString("zh-CN")}
                                  </TableCell>
                                </TableRow>
                              ))}
                            </TableBody>
                          </Table>
                          <PaginationBar
                            page={selectedExecutionList.page}
                            pageSize={selectedExecutionList.page_size}
                            total={selectedExecutionList.total}
                            onPrev={() =>
                              setPageState((prev) => ({
                                ...prev,
                                [executionTab === "today" ? "todayExecutions" : "historyExecutions"]:
                                  Math.max(
                                    1,
                                    prev[executionTab === "today" ? "todayExecutions" : "historyExecutions"] - 1,
                                  ),
                              }))
                            }
                            onNext={() =>
                              setPageState((prev) => ({
                                ...prev,
                                [executionTab === "today" ? "todayExecutions" : "historyExecutions"]:
                                  prev[executionTab === "today" ? "todayExecutions" : "historyExecutions"] + 1,
                              }))
                            }
                          />
                        </>
                      )}
                    </div>
                  ) : null}

                  {mainTab === "cashflows" ? (
                    sectionError.cashFlows ? (
                      <EmptyState title="资金流水查询失败" detail={sectionError.cashFlows} />
                    ) : !cashFlows || cashFlows.items.length === 0 ? (
                      <EmptyState
                        title="暂无资金流水"
                        detail="买卖成交、资金变化和账户流水会在 Longbridge 支持范围内同步展示。"
                      />
                    ) : (
                      <>
                        <Table>
                          <TableHeader>
                            <TableRow>
                              <TableHead>类型</TableHead>
                              <TableHead>说明</TableHead>
                              <TableHead className="text-right">金额</TableHead>
                              <TableHead className="text-right">余额</TableHead>
                              <TableHead>时间</TableHead>
                            </TableRow>
                          </TableHeader>
                          <TableBody>
                            {cashFlows.items.map((flow) => (
                              <TableRow key={flow.cash_flow_id}>
                                <TableCell className="py-3 uppercase text-muted-foreground">
                                  {flow.business_type}
                                </TableCell>
                                <TableCell className="py-3">
                                  <div className="font-medium text-foreground">{flow.description}</div>
                                  {flow.symbol ? (
                                    <div className="text-xs text-muted-foreground">{flow.symbol}</div>
                                  ) : null}
                                </TableCell>
                                <TableCell
                                  className={cn(
                                    "py-3 text-right font-mono font-semibold",
                                    flow.amount >= 0 ? "text-emerald-400" : "text-red-400",
                                  )}
                                >
                                  {formatSignedMoney(flow.amount, flow.currency)}
                                </TableCell>
                                <TableCell className="py-3 text-right font-mono">
                                  {formatMoney(flow.balance, flow.currency)}
                                </TableCell>
                                <TableCell className="py-3 text-xs text-muted-foreground">
                                  {new Date(flow.occurred_at).toLocaleString("zh-CN")}
                                </TableCell>
                              </TableRow>
                            ))}
                          </TableBody>
                        </Table>
                        <PaginationBar
                          page={cashFlows.page}
                          pageSize={cashFlows.page_size}
                          total={cashFlows.total}
                          onPrev={() =>
                            setPageState((prev) => ({ ...prev, cashFlows: Math.max(1, prev.cashFlows - 1) }))
                          }
                          onNext={() =>
                            setPageState((prev) => ({ ...prev, cashFlows: prev.cashFlows + 1 }))
                          }
                        />
                      </>
                    )
                  ) : null}
                </CardContent>
                <FeatureGuideButton guideKey={detailGuideKey} className="bottom-4 right-4" />
              </Card>

              {selectedOrder ? (
                <Card className="border-border bg-card/40">
                  <CardHeader className="pb-3">
                    <CardTitle className="text-lg font-semibold">订单详情</CardTitle>
                  </CardHeader>
                  <CardContent className="grid gap-3 md:grid-cols-2">
                    <SummaryItem label="订单号" value={selectedOrder.order_id} mono />
                    <SummaryItem label="状态" value={orderStatusLabel(selectedOrder.status)} />
                    <SummaryItem label="标的" value={`${selectedOrder.symbol} · ${selectedOrder.name}`} />
                    <SummaryItem label="方向" value={selectedOrder.side === "buy" ? "买入" : "卖出"} />
                    <SummaryItem label="订单类型" value={selectedOrder.order_type === "market" ? "市价" : "限价"} />
                    <SummaryItem label="委托数量" value={`${selectedOrder.quantity}`} mono />
                    <SummaryItem label="已成交数量" value={`${selectedOrder.executed_quantity}`} mono />
                    <SummaryItem
                      label="委托价格"
                      value={selectedOrder.submitted_price != null ? `${selectedOrder.submitted_price}` : "—"}
                      mono
                    />
                    <SummaryItem
                      label="成交均价"
                      value={selectedOrder.executed_price != null ? `${selectedOrder.executed_price}` : "—"}
                      mono
                    />
                    <SummaryItem label="提交时间" value={new Date(selectedOrder.submitted_at).toLocaleString("zh-CN")} />
                    <SummaryItem label="更新时间" value={new Date(selectedOrder.updated_at).toLocaleString("zh-CN")} />
                    <SummaryItem label="备注" value={selectedOrder.message ?? "—"} />
                  </CardContent>
                  {selectedOrderEvents.length > 0 ? (
                    <CardContent className="pt-0">
                      <div className="mb-3 text-sm font-medium text-foreground">状态时间线</div>
                      <div className="space-y-2">
                        {selectedOrderEvents.map((event) => (
                          <div
                            key={event.event_id}
                            className="flex items-start justify-between gap-3 rounded-2xl border border-border bg-background/40 px-3 py-3"
                          >
                            <div>
                              <div className="text-sm font-medium text-foreground">
                                {orderStatusLabel(event.status)}
                              </div>
                              <div className="text-xs text-muted-foreground">
                                {event.message || event.event_type}
                              </div>
                            </div>
                            <div className="text-right text-xs text-muted-foreground">
                              {new Date(event.occurred_at).toLocaleString("zh-CN")}
                            </div>
                          </div>
                        ))}
                      </div>
                    </CardContent>
                  ) : null}
                  {selectedOrderReport ? (
                    <CardContent className="pt-0">
                      <div className="mb-3 text-sm font-medium text-foreground">执行摘要</div>
                      <div className="grid gap-3 md:grid-cols-2">
                        <SummaryItem
                          label="成交率"
                          value={`${(selectedOrderReport.fill_ratio * 100).toFixed(1)}%`}
                          mono
                        />
                        <SummaryItem
                          label="生命周期"
                          value={`${selectedOrderReport.lifecycle_seconds.toFixed(2)}s`}
                          mono
                        />
                        <SummaryItem
                          label="事件数"
                          value={`${selectedOrderReport.event_count}`}
                          mono
                        />
                        <SummaryItem
                          label="成交笔数"
                          value={`${selectedOrderReport.execution_count}`}
                          mono
                        />
                        <SummaryItem
                          label="平均成交价"
                          value={
                            selectedOrderReport.avg_execution_price != null
                              ? `${selectedOrderReport.avg_execution_price.toFixed(4)}`
                              : "—"
                          }
                          mono
                        />
                        <SummaryItem
                          label="成交耗时"
                          value={`${selectedOrderReport.execution_span_seconds.toFixed(2)}s`}
                          mono
                        />
                        <SummaryItem
                          label="价格偏差"
                          value={
                            selectedOrderReport.price_delta != null
                              ? `${selectedOrderReport.price_delta >= 0 ? "+" : ""}${selectedOrderReport.price_delta.toFixed(4)}`
                              : "—"
                          }
                          mono
                        />
                        <SummaryItem
                          label="首笔成交"
                          value={
                            selectedOrderReport.first_execution_at
                              ? new Date(selectedOrderReport.first_execution_at).toLocaleString("zh-CN")
                              : "—"
                          }
                        />
                        <SummaryItem
                          label="末笔成交"
                          value={
                            selectedOrderReport.last_execution_at
                              ? new Date(selectedOrderReport.last_execution_at).toLocaleString("zh-CN")
                              : "—"
                          }
                        />
                      </div>
                    </CardContent>
                  ) : null}
                </Card>
              ) : null}
            </div>
          </div>
        </>
      )}

      <FeatureGuideButton guideKey="trading.execution" className="bottom-5 right-5" />

      <ConfirmDialog
        open={confirm.open}
        onClose={() => setConfirm({ open: false, submitting: false })}
        onConfirm={() => void performSubmit()}
        submitting={confirm.submitting}
        summary={orderSummary}
      />
    </div>
  );
}

function SummaryItem({
  label,
  value,
  valueClass,
  mono = false,
}: {
  label: string;
  value: string;
  valueClass?: string;
  mono?: boolean;
}) {
  return (
    <div className="rounded-2xl border border-border bg-background/40 px-3 py-3">
      <div className="text-[11px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className={cn("mt-1 text-sm font-medium text-foreground", mono && "font-mono", valueClass)}>
        {value}
      </div>
    </div>
  );
}

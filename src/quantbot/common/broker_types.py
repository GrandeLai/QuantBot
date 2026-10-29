"""交易 Provider 统一类型定义."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class TradingProviderKind(StrEnum):
    """交易 provider 类型."""

    FUTU = "futu"
    LONGBRIDGE = "longbridge"
    MOCK = "mock"
    OKX = "okx"


class TradingMode(StrEnum):
    """交易环境."""

    PAPER = "paper"


class TradingMarket(StrEnum):
    """支持的交易市场."""

    US = "US"
    HK = "HK"
    CRYPTO = "CRYPTO"
    UNKNOWN = "UNKNOWN"


class TradingAssetType(StrEnum):
    """资产类型."""

    STOCK = "stock"
    ETF = "etf"
    WARRANT = "warrant"
    OPTION = "option"
    OTC = "otc"
    CRYPTO = "crypto"
    UNKNOWN = "unknown"


class TradingOrderSide(StrEnum):
    """交易方向."""

    BUY = "buy"
    SELL = "sell"


class TradingOrderType(StrEnum):
    """当前启用的订单类型.

    以 broker sandbox/testnet 能力为边界，当前版本优先启用常规
    market / limit 两类标准订单，不伪装支持未落地的复杂条件单流程。
    """

    MARKET = "market"
    LIMIT = "limit"


class TradingOrderStatus(StrEnum):
    """统一订单状态."""

    PENDING_SUBMIT = "pending_submit"
    SUBMITTED = "submitted"
    PARTIAL_FILLED = "partial_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class TradingOrderEventType(StrEnum):
    """订单事件类型."""

    SUBMITTED = "submitted"
    PARTIAL_FILLED = "partial_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    RISK_REJECTED = "risk_rejected"
    UPDATED = "updated"


class TradingSessionStatus(StrEnum):
    """交易时段状态."""

    REGULAR = "regular"
    PRE_MARKET = "pre_market"
    POST_MARKET = "post_market"
    CLOSED = "closed"
    MIDDAY_BREAK = "midday_break"
    UNKNOWN = "unknown"


class TradingCapability(BaseModel):
    """Broker sandbox/testnet 能力边界."""

    supported_markets: list[TradingMarket]
    supported_asset_types: list[TradingAssetType]
    supported_order_types: list[TradingOrderType]
    supports_us_short_selling: bool = True
    supports_otc: bool = False
    supports_us_prepost: bool = False
    supports_options: bool = False
    notes: list[str] = Field(default_factory=list)


class TradingProviderStatus(BaseModel):
    """当前交易 provider 状态."""

    provider: TradingProviderKind
    mode: TradingMode = TradingMode.PAPER
    configured: bool
    using_mock_fallback: bool = False
    reason: str | None = None
    capabilities: TradingCapability


class TradingSecurity(BaseModel):
    """可交易标的基础信息."""

    symbol: str
    name: str
    market: TradingMarket
    currency: str
    asset_type: TradingAssetType = TradingAssetType.UNKNOWN
    lot_size: int = 1
    tradeable: bool = True
    shortable: bool = False
    restrictions: list[str] = Field(default_factory=list)


class TradingQuote(BaseModel):
    """行情快照."""

    symbol: str
    name: str
    market: TradingMarket
    currency: str
    asset_type: TradingAssetType = TradingAssetType.UNKNOWN
    last_price: float
    prev_close: float
    change: float
    change_pct: float
    trade_session: TradingSessionStatus = TradingSessionStatus.UNKNOWN
    trade_status: str = "unknown"
    tradeable: bool = True
    restrictions: list[str] = Field(default_factory=list)


class TradingCashInfo(BaseModel):
    """按币种展示的现金信息."""

    currency: str
    available_cash: float = 0.0
    withdraw_cash: float = 0.0
    frozen_cash: float = 0.0
    settling_cash: float = 0.0


class TradingAccountOverview(BaseModel):
    """账户总览."""

    provider: TradingProviderKind
    mode: TradingMode = TradingMode.PAPER
    currency: str = "USD"
    total_assets: float = 0.0
    available_cash: float = 0.0
    withdrawable_cash: float = 0.0
    buying_power: float = 0.0
    positions_market_value: float = 0.0
    today_pnl: float = 0.0
    today_pnl_pct: float = 0.0
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0
    cash_infos: list[TradingCashInfo] = Field(default_factory=list)
    updated_at: str
    warnings: list[str] = Field(default_factory=list)


class TradingPosition(BaseModel):
    """持仓条目."""

    symbol: str
    name: str
    market: TradingMarket
    currency: str
    asset_type: TradingAssetType = TradingAssetType.UNKNOWN
    quantity: int
    available_quantity: int
    cost_price: float
    last_price: float
    market_value: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    day_pnl: float = 0.0
    day_pnl_pct: float = 0.0


class TradingOrder(BaseModel):
    """订单条目."""

    order_id: str
    symbol: str
    name: str
    market: TradingMarket
    currency: str
    asset_type: TradingAssetType = TradingAssetType.UNKNOWN
    side: TradingOrderSide
    order_type: TradingOrderType
    status: TradingOrderStatus
    quantity: int
    executed_quantity: int = 0
    submitted_price: float | None = None
    trigger_price: float | None = None
    executed_price: float | None = None
    submitted_at: str
    updated_at: str
    message: str | None = None


class TradingExecution(BaseModel):
    """成交条目."""

    execution_id: str
    order_id: str
    symbol: str
    name: str
    market: TradingMarket
    currency: str
    asset_type: TradingAssetType = TradingAssetType.UNKNOWN
    side: TradingOrderSide
    price: float
    quantity: int
    executed_at: str


class TradingOrderEvent(BaseModel):
    """订单事件时间线条目."""

    event_id: str
    order_id: str
    event_type: TradingOrderEventType
    status: TradingOrderStatus
    message: str
    occurred_at: str


class TradingExecutionReport(BaseModel):
    """订单执行报告摘要."""

    order_id: str
    status: TradingOrderStatus
    submitted_quantity: int
    executed_quantity: int
    fill_ratio: float
    event_count: int
    lifecycle_seconds: float
    execution_count: int = 0
    avg_execution_price: float | None = None
    first_execution_at: str | None = None
    last_execution_at: str | None = None
    execution_span_seconds: float = 0.0
    price_delta: float | None = None
    slippage_bps: float | None = None


class TradingRiskStatus(BaseModel):
    """交易风控状态摘要."""

    enabled: bool
    halted: bool
    max_position_count: int
    max_single_position_pct: float
    daily_loss_limit_pct: float | None = None
    max_order_value: float | None = None
    current_today_pnl_pct: float = 0.0
    open_position_count: int = 0
    available_position_slots: int = 0
    largest_position_symbol: str | None = None
    largest_position_ratio: float = 0.0
    warnings: list[str] = Field(default_factory=list)


class TradingCashFlow(BaseModel):
    """资金流水条目."""

    cash_flow_id: str
    currency: str
    amount: float
    balance: float
    business_type: str
    direction: str
    description: str
    symbol: str | None = None
    occurred_at: str


class TradingOrderEstimate(BaseModel):
    """下单前估算结果."""

    symbol: str
    side: TradingOrderSide
    order_type: TradingOrderType
    reference_price: float
    cash_max_qty: int = 0
    sell_max_qty: int = 0
    reason: str | None = None


class TradingOrderRequest(BaseModel):
    """统一下单请求."""

    symbol: str
    side: TradingOrderSide
    order_type: TradingOrderType
    quantity: int = Field(..., gt=0)
    submitted_price: float | None = Field(default=None, gt=0)
    trigger_price: float | None = Field(default=None, gt=0)


class TradingOrderEstimateRequest(BaseModel):
    """下单前估算请求."""

    symbol: str
    side: TradingOrderSide
    order_type: TradingOrderType
    submitted_price: float | None = Field(default=None, gt=0)
    trigger_price: float | None = Field(default=None, gt=0)


class TradingSubmitResult(BaseModel):
    """下单结果."""

    provider: TradingProviderKind
    order_id: str
    status: TradingOrderStatus
    message: str


class TradingCancelResult(BaseModel):
    """撤单结果."""

    provider: TradingProviderKind
    order_id: str
    status: TradingOrderStatus
    message: str


# ── Crypto 专属类型（数量为 float，兼容小数）─────────────────────────────────

class CryptoBalance(BaseModel):
    """账户余额条目."""

    asset: str
    free: float
    locked: float
    total: float


class CryptoOrder(BaseModel):
    """加密货币订单."""

    order_id: str
    symbol: str
    display: str
    side: TradingOrderSide
    order_type: TradingOrderType
    status: TradingOrderStatus
    quantity: float
    executed_quantity: float = 0.0
    submitted_price: float | None = None
    executed_price: float | None = None
    submitted_at: str
    updated_at: str


class CryptoOrderRequest(BaseModel):
    """加密货币下单请求."""

    symbol: str
    side: TradingOrderSide
    order_type: TradingOrderType
    quantity: float = Field(..., gt=0)
    price: float | None = Field(default=None, gt=0)


class CryptoAccountOverview(BaseModel):
    """加密账户总览."""

    provider: TradingProviderKind = TradingProviderKind.OKX
    testnet: bool = True
    balances: list[CryptoBalance] = Field(default_factory=list)
    total_usdt_value: float = 0.0
    updated_at: str


class CryptoPosition(BaseModel):
    """衍生品持仓（合约 / 期权）."""

    pos_id: str = ""
    inst_id: str
    inst_type: str           # SWAP / FUTURES / OPTION
    pos_side: str            # long / short / net
    pos: float               # 持仓数量（张数）
    avg_px: float            # 开仓均价
    mark_px: float           # 最新标记价格
    upl: float               # 未实现盈亏
    upl_ratio: float         # 未实现盈亏率
    lever: str               # 杠杆倍数
    liq_px: float | None = None  # 预估强平价
    margin_mode: str         # cross / isolated
    currency: str = ""       # 保证金币种
    updated_at: str = ""


class SwapTicker(BaseModel):
    """永续合约行情."""

    inst_id: str
    display: str
    last: float
    mark_px: float
    change_pct: float
    funding_rate: float
    next_funding_time: str
    open_interest: float
    volume_usdt: float
    high_24h: float
    low_24h: float


class OptionTicker(BaseModel):
    """期权行情（含希腊字母）."""

    inst_id: str
    uly: str
    strike_px: float
    opt_type: str        # C / P
    exp_time: str        # YYYYMMDD
    last: float
    bid_px: float
    ask_px: float
    mark_vol: float      # 隐含波动率（OKX markVol）
    delta: float
    gamma: float
    theta: float
    vega: float
    open_interest: float = 0.0


class SwapOrderRequest(BaseModel):
    """永续合约下单请求."""

    inst_id: str
    side: TradingOrderSide
    order_type: TradingOrderType
    sz: float = Field(..., gt=0)
    price: float | None = Field(default=None, gt=0)
    pos_side: str = "long"    # long / short / net
    margin_mode: str = "cross"
    lever: str = "10"


class TradingProviderError(Exception):
    """交易 Provider 统一异常."""

    def __init__(self, message: str, *, code: str = "provider_error", status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code

"""税务亏损收割（TLH）核心引擎（Phase F.3）.

功能：
- 扫描持仓 tax lots，识别满足最小亏损阈值的候选
- 检测 wash sale 风险（近 30 天内有同 ticker 买入记录）
- 提供替代 ETF 推荐，避免 substantially identical 禁买
- 估算通过 harvest 亏损可节约的税额

合规声明：
本模块输出仅供参考，不构成税务建议，请咨询 CPA 确认具体情况。

数据来源：规则硬编码（无外部 API 依赖）
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_WASH_SALE_WINDOW_DAYS = 30
_LONG_TERM_THRESHOLD_DAYS = 365

# ETF 替代表（保守推荐，相关性高但非 substantially identical）
# 个股 → 对应行业 ETF（用户应自行确认 IRS 认定边界）
_REPLACEMENT_MAP: dict[str, list[str]] = {
    # 宽基 ETF 互换（IRS 尚未明确是否 substantially identical，保守使用）
    "SPY": ["VOO", "IVV", "SPLG"],
    "VOO": ["SPY", "IVV", "SPLG"],
    "IVV": ["SPY", "VOO", "SPLG"],
    "SPLG": ["SPY", "VOO", "IVV"],
    "QQQ": ["QQQM", "ONEQ"],
    "QQQM": ["QQQ", "ONEQ"],
    "ONEQ": ["QQQ", "QQQM"],
    "IWM": ["VTWO", "SCHA"],
    "VTWO": ["IWM", "SCHA"],
    "SCHA": ["IWM", "VTWO"],
    "VTI": ["ITOT", "SCHB"],
    "ITOT": ["VTI", "SCHB"],
    "SCHB": ["VTI", "ITOT"],
    "AGG": ["BND", "SCHZ"],
    "BND": ["AGG", "SCHZ"],
    "SCHZ": ["AGG", "BND"],
    "EFA": ["VEA", "IEFA"],
    "VEA": ["EFA", "IEFA"],
    "IEFA": ["EFA", "VEA"],
    "EEM": ["VWO", "IEMG"],
    "VWO": ["EEM", "IEMG"],
    "IEMG": ["EEM", "VWO"],
    # 个股 → 对应行业 ETF（非 substantially identical，但需用户确认）
    "AAPL": ["XLK", "VGT"],
    "MSFT": ["XLK", "VGT"],
    "NVDA": ["SOXX", "SMH"],
    "AMD": ["SOXX", "SMH"],
    "INTC": ["SOXX", "SMH"],
    "TSLA": ["XLY", "CARZ"],
    "META": ["XLC", "IYC"],
    "GOOGL": ["XLC", "IYC"],
    "GOOG": ["XLC", "IYC"],
    "AMZN": ["XLY", "IBUY"],
    "NFLX": ["XLC", "IYC"],
    "JPM": ["XLF", "KBE"],
    "BAC": ["XLF", "KBE"],
    "GS": ["XLF", "KBE"],
    "XOM": ["XLE", "VDE"],
    "CVX": ["XLE", "VDE"],
    "JNJ": ["XLV", "VHT"],
    "PFE": ["XLV", "VHT"],
    "UNH": ["XLV", "VHT"],
    "PG": ["XLP", "VDC"],
    "KO": ["XLP", "VDC"],
    "WMT": ["XLP", "VDC"],
}


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class TaxLot:
    """单个持仓 lot（来自 broker tax lot 数据）."""

    ticker: str
    quantity: float          # 持仓数量（股）
    cost_basis: float        # 每股成本（$）
    acquisition_date: date   # 买入日期
    lot_id: str              # 唯一 ID（broker 端）


@dataclass
class TLHCandidate:
    """TLH 候选仓位（浮亏满足阈值）."""

    lot: TaxLot
    current_price: float
    unrealized_pnl: float         # current_price × qty - cost_basis × qty（负值）
    unrealized_pnl_pct: float     # unrealized_pnl / (cost_basis × qty)（负值）
    holding_days: int
    is_long_term: bool            # holding_days >= 365
    replacement_tickers: list[str]   # 推荐替代 ETF
    wash_sale_risk: bool          # True → 近 30 天内有买入记录


@dataclass
class WashSaleWarning:
    """Wash sale 风险警告."""

    ticker: str
    last_purchase_date: date
    days_since_purchase: int      # 若 < 30 则存在 wash sale 风险


# ---------------------------------------------------------------------------
# 核心函数
# ---------------------------------------------------------------------------


def get_replacement_tickers(ticker: str) -> list[str]:
    """查询替代 ETF 列表.

    对未知 ticker 返回空列表（用户需自行找替代品）。
    """
    return _REPLACEMENT_MAP.get(ticker.upper(), [])


def _check_wash_sale(
    ticker: str,
    recent_purchases: dict[str, date],
    *,
    window_days: int = _WASH_SALE_WINDOW_DAYS,
    reference_date: date | None = None,
) -> bool:
    """检测该 ticker 是否在 wash sale 窗口内有买入记录.

    Args:
        ticker: 证券代码
        recent_purchases: {ticker: 最近买入日期}（来自用户/broker 输入）
        window_days: wash sale 禁买窗口天数，默认 30
        reference_date: 基准日期（None → 使用 today）

    Returns:
        True 表示存在 wash sale 风险
    """
    last_purchase = recent_purchases.get(ticker.upper())
    if last_purchase is None:
        return False
    ref = reference_date or date.today()
    days_since = (ref - last_purchase).days
    return days_since < window_days


def scan_tlh_candidates(
    lots: list[TaxLot],
    current_prices: dict[str, float],
    recent_purchases: dict[str, date],
    *,
    min_loss_pct: float = -0.05,   # 只推荐跌幅 >= 5% 的（负值）
    min_loss_usd: float = 500.0,   # 只推荐亏损 >= $500 的（正值，绝对值）
    reference_date: date | None = None,
) -> list[TLHCandidate]:
    """扫描 tax lots，返回满足 TLH 条件的候选列表.

    候选条件（均需满足）：
    1. unrealized_pnl_pct <= min_loss_pct（默认 -5%）
    2. abs(unrealized_pnl) >= min_loss_usd（默认 $500）

    候选按亏损金额从大到小排序（优先 harvest 最大亏损）。

    Args:
        lots: 持仓 lot 列表
        current_prices: {ticker: 当前价格（$）}
        recent_purchases: {ticker: 最近买入日期}（用于 wash sale 检查）
        min_loss_pct: 最小跌幅阈值（负数，如 -0.05 表示 -5%）
        min_loss_usd: 最小亏损金额阈值（正数，单位 $）
        reference_date: 持有天数计算基准日期（None → today）

    Returns:
        TLHCandidate 列表，按 unrealized_pnl 升序（最大亏损优先）
    """
    ref = reference_date or date.today()
    candidates: list[TLHCandidate] = []

    for lot in lots:
        ticker_upper = lot.ticker.upper()
        current_price = current_prices.get(ticker_upper)
        if current_price is None:
            # 缺少价格数据，跳过
            continue

        cost_total = lot.cost_basis * lot.quantity
        current_total = current_price * lot.quantity
        unrealized_pnl = current_total - cost_total

        if cost_total <= 0:
            continue

        unrealized_pnl_pct = unrealized_pnl / cost_total

        # 过滤：必须有亏损且满足阈值
        if unrealized_pnl_pct > min_loss_pct:
            continue
        if abs(unrealized_pnl) < min_loss_usd:
            continue

        holding_days = (ref - lot.acquisition_date).days
        is_long_term = holding_days >= _LONG_TERM_THRESHOLD_DAYS

        replacement_tickers = get_replacement_tickers(ticker_upper)
        wash_sale_risk = _check_wash_sale(
            ticker_upper, recent_purchases, reference_date=ref
        )

        candidates.append(
            TLHCandidate(
                lot=lot,
                current_price=current_price,
                unrealized_pnl=unrealized_pnl,
                unrealized_pnl_pct=unrealized_pnl_pct,
                holding_days=holding_days,
                is_long_term=is_long_term,
                replacement_tickers=replacement_tickers,
                wash_sale_risk=wash_sale_risk,
            )
        )

    # 按亏损金额从大到小排序（unrealized_pnl 最负的排前面）
    candidates.sort(key=lambda c: c.unrealized_pnl)
    return candidates


def estimate_tax_saving(
    candidates: list[TLHCandidate],
    *,
    short_term_rate: float = 0.37,   # 短期资本利得税率（用户可调）
    long_term_rate: float = 0.20,    # 长期资本利得税率
) -> float:
    """估算通过 harvest 这些亏损可节约的税额（$）.

    税务逻辑：
    - 亏损用于抵扣同类型增益（短期抵短期、长期抵长期）
    - 节税额 = 亏损金额（绝对值）× 对应税率

    免责声明：此为估算，实际税务处理取决于用户整体税务情况，
    请咨询 CPA 确认。

    Args:
        candidates: TLH 候选列表（来自 scan_tlh_candidates）
        short_term_rate: 短期资本利得边际税率（默认 0.37）
        long_term_rate: 长期资本利得税率（默认 0.20）

    Returns:
        预计节税金额（$），始终 >= 0
    """
    total_saving = 0.0
    for c in candidates:
        loss_amount = abs(c.unrealized_pnl)  # 亏损金额（正值）
        rate = long_term_rate if c.is_long_term else short_term_rate
        total_saving += loss_amount * rate
    return round(total_saving, 2)

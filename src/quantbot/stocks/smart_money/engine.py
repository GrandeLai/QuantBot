"""Smart Money Flow Engine — 大单不对称积分（机构资金流向识别）.

Phase F.22 — Smart Money Flow
数据来源：yfinance 5 天 1 分钟 K 线（免费，美股最近 7 天内）

算法：
1. 拉取 5 天 1 分钟 OHLCV 数据
2. 计算每 bar 的美元成交额 = Volume × VWAP（≈ (H+L+C)/3 × Vol）
3. 识别"大单 bar"：美元成交额 > 2× 5 天中位数
4. 大单方向分类（简化 Lee-Ready tick rule）：
   - close > open → 买方主导（bullish bar）
   - close < open → 卖方主导（bearish bar）
   - close == open → 中性（不计入）
5. 计算今日大单买压比 = 大单买入$额 / (大单买入$额 + 大单卖出$额)
6. 对比 5 天均值得出趋势信号

信号定义：
- smart_money_buy: 今日买压比 > 60% 且高于 5 日均值 10+ pp
- smart_money_sell: 今日买压比 < 40% 且低于 5 日均值 10- pp
- accumulation: 今日买压比 > 55%（温和积累）
- distribution: 今日买压比 < 45%（温和派发）
- neutral: 40-60% 且与历史均值接近
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import yfinance as yf
import pandas as pd

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

SmartMoneySignal = Literal[
    "smart_money_buy",
    "smart_money_sell",
    "accumulation",
    "distribution",
    "neutral",
    "no_data",
]


@dataclass
class DailyFlow:
    date: str
    large_buy_usd: float
    large_sell_usd: float
    buy_pressure_pct: float   # 0-100
    total_large_usd: float
    large_bar_count: int


@dataclass
class SmartMoneyData:
    ticker: str
    signal: SmartMoneySignal
    today_buy_pressure_pct: float | None   # 0-100
    avg_5d_buy_pressure_pct: float | None  # 5-day average
    large_threshold_usd: float | None      # large bar threshold in USD
    daily_flows: list[DailyFlow] = field(default_factory=list)
    interpretation: str = ""
    as_of_date: date = field(default_factory=date.today)
    data_available: bool = True


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LARGE_MULTIPLIER = 2.0   # bar must be > 2× median to be "large"
_BUY_STRONG_THRESHOLD = 60.0
_SELL_STRONG_THRESHOLD = 40.0
_SIGNAL_DELTA = 10.0      # pp difference from 5d avg to qualify as strong signal


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def _classify_bars(df: pd.DataFrame, threshold_usd: float) -> tuple[float, float, int]:
    """
    Given a OHLCV DataFrame and a large-bar threshold,
    return (large_buy_usd, large_sell_usd, large_bar_count).
    """
    large_buy = 0.0
    large_sell = 0.0
    count = 0

    for _, row in df.iterrows():
        try:
            vwap = (float(row["High"]) + float(row["Low"]) + float(row["Close"])) / 3.0
            vol = float(row["Volume"])
            usd = vwap * vol
        except (KeyError, TypeError, ValueError):
            continue

        if usd < threshold_usd:
            continue

        count += 1
        open_ = float(row["Open"])
        close = float(row["Close"])

        if close > open_:
            large_buy += usd
        elif close < open_:
            large_sell += usd
        # neutral (close == open) → skip

    return large_buy, large_sell, count


def _buy_pressure(buy_usd: float, sell_usd: float) -> float | None:
    total = buy_usd + sell_usd
    if total == 0:
        return None
    return (buy_usd / total) * 100.0


def _compute_signal_from_pressure(
    today_pct: float | None,
    avg_5d_pct: float | None,
) -> SmartMoneySignal:
    if today_pct is None:
        return "no_data"

    delta = (today_pct - avg_5d_pct) if avg_5d_pct is not None else 0.0

    if today_pct > _BUY_STRONG_THRESHOLD and delta >= _SIGNAL_DELTA:
        return "smart_money_buy"
    if today_pct < _SELL_STRONG_THRESHOLD and delta <= -_SIGNAL_DELTA:
        return "smart_money_sell"
    if today_pct > 55.0:
        return "accumulation"
    if today_pct < 45.0:
        return "distribution"
    return "neutral"


def _build_interpretation(
    signal: SmartMoneySignal,
    today_pct: float | None,
    avg_5d_pct: float | None,
    threshold_usd: float | None,
) -> str:
    def _fmt(v: float | None) -> str:
        return f"{v:.1f}%" if v is not None else "N/A"

    thresh_str = f"${threshold_usd:,.0f}" if threshold_usd else "N/A"

    if signal == "smart_money_buy":
        return (
            f"✓ 大单买压强烈：今日大单买压比 {_fmt(today_pct)}，"
            f"高于 5 日均值 {_fmt(avg_5d_pct)} 超 10 pp。"
            "机构资金近期可能在积极建仓，建议关注后续成交量变化确认。"
            f"（大单阈值：{thresh_str}/bar）"
        )
    if signal == "smart_money_sell":
        return (
            f"⚠ 大单卖压显著：今日大单买压比 {_fmt(today_pct)}，"
            f"低于 5 日均值 {_fmt(avg_5d_pct)} 超 10 pp。"
            "机构资金可能在派发仓位，需警惕价格承压风险。"
            f"（大单阈值：{thresh_str}/bar）"
        )
    if signal == "accumulation":
        return (
            f"大单买压温和偏强（{_fmt(today_pct)}），显示资金缓慢积累。"
            "信号尚未达到集中建仓程度，建议配合价格趋势确认。"
        )
    if signal == "distribution":
        return (
            f"大单卖压温和偏强（{_fmt(today_pct)}），显示资金缓慢派发。"
            "建议观察是否有后续加速卖出迹象。"
        )
    if signal == "no_data":
        return "无法获取分钟级数据（yfinance 仅支持最近 7 天 1 分钟 K 线）。"
    return (
        f"大单买压比 {_fmt(today_pct)}（5 日均值 {_fmt(avg_5d_pct)}），"
        "机构资金流向暂无明显方向性偏差。"
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compute_smart_money(ticker: str) -> SmartMoneyData:
    """
    Compute smart money flow signal from 1-minute OHLCV data.
    Always returns; never raises.
    Uses yfinance 1m interval (last 5 trading days).
    """
    ticker = ticker.strip().upper()

    try:
        raw = yf.download(
            tickers=ticker,
            period="5d",
            interval="1m",
            auto_adjust=True,
            progress=False,
        )

        if raw is None or raw.empty:
            return SmartMoneyData(
                ticker=ticker,
                signal="no_data",
                today_buy_pressure_pct=None,
                avg_5d_buy_pressure_pct=None,
                large_threshold_usd=None,
                data_available=False,
                interpretation="分钟级数据不可用（yfinance 无法获取 1 分钟 K 线）。",
            )

        # Handle MultiIndex columns from yf.download
        if isinstance(raw.columns, pd.MultiIndex):
            raw = raw.xs(ticker, axis=1, level=1) if ticker in raw.columns.get_level_values(1) else raw.droplevel(1, axis=1)

        # Compute dollar volume per bar
        raw["vwap"] = (raw["High"] + raw["Low"] + raw["Close"]) / 3.0
        raw["dollar_vol"] = raw["vwap"] * raw["Volume"]

        # Compute median threshold across all bars
        median_usd = float(raw["dollar_vol"].median())
        threshold_usd = median_usd * _LARGE_MULTIPLIER

        # Group by trading date
        raw["_date"] = pd.to_datetime(raw.index).date
        daily_flows: list[DailyFlow] = []

        for day, group in raw.groupby("_date"):
            buy_usd, sell_usd, cnt = _classify_bars(group, threshold_usd)
            bp = _buy_pressure(buy_usd, sell_usd)
            daily_flows.append(
                DailyFlow(
                    date=str(day),
                    large_buy_usd=buy_usd,
                    large_sell_usd=sell_usd,
                    buy_pressure_pct=bp if bp is not None else 50.0,
                    total_large_usd=buy_usd + sell_usd,
                    large_bar_count=cnt,
                )
            )

        daily_flows.sort(key=lambda d: d.date)

        if not daily_flows:
            return SmartMoneyData(
                ticker=ticker,
                signal="no_data",
                today_buy_pressure_pct=None,
                avg_5d_buy_pressure_pct=None,
                large_threshold_usd=threshold_usd,
                data_available=True,
                interpretation="数据不足，无法计算大单流向。",
            )

        today_flow = daily_flows[-1]
        today_pct = (
            today_flow.buy_pressure_pct
            if today_flow.total_large_usd > 0
            else None
        )

        # 5-day average (exclude today if we have enough days)
        prior_days = daily_flows[:-1] if len(daily_flows) > 1 else daily_flows
        valid_prior = [d for d in prior_days if d.total_large_usd > 0]
        avg_5d = (
            sum(d.buy_pressure_pct for d in valid_prior) / len(valid_prior)
            if valid_prior
            else None
        )

        signal = _compute_signal_from_pressure(today_pct, avg_5d)
        interpretation = _build_interpretation(signal, today_pct, avg_5d, threshold_usd)

        return SmartMoneyData(
            ticker=ticker,
            signal=signal,
            today_buy_pressure_pct=today_pct,
            avg_5d_buy_pressure_pct=avg_5d,
            large_threshold_usd=threshold_usd,
            daily_flows=daily_flows,
            interpretation=interpretation,
            as_of_date=date.today(),
            data_available=True,
        )

    except Exception:  # noqa: BLE001
        return SmartMoneyData(
            ticker=ticker,
            signal="no_data",
            today_buy_pressure_pct=None,
            avg_5d_buy_pressure_pct=None,
            large_threshold_usd=None,
            data_available=False,
            interpretation="查询失败，请稍后重试。",
        )

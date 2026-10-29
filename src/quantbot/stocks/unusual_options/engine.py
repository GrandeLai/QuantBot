"""Unusual Options Activity (UOA) engine.

Scans yfinance option chains for contracts where volume >> open interest,
which signals potential institutional pre-positioning.

Methodology:
- volume_oi_ratio = volume / max(1, open_interest)
- Ratio > 3.0 flags a contract as "unusual"
- Aggregate unusual call vs put counts → bullish/bearish/mixed grade

Graceful degradation: always returns UnusualOptionsData (never None, never
raises). data_available=False when yfinance cannot provide options data.

Reference:
    Barchart Unusual Activity, Unusual Whales methodology;
    Easley, O'Hara & Srinivas (1998) "Option Volume and Stock Prices"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

import pandas as pd
import yfinance as yf
from loguru import logger

OptionsGrade = Literal["bullish_unusual", "bearish_unusual", "mixed_unusual", "neutral"]

_VOLUME_OI_THRESHOLD = 3.0   # volume / OI ratio to flag as unusual
_MAX_EXPIRIES = 3            # scan only the nearest N expiry dates
_TOP_N = 5                   # top unusual contracts to surface


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class UnusualContract:
    """Single options contract flagged as unusual."""

    ticker: str
    expiry: str           # "YYYY-MM-DD"
    strike: float
    option_type: str      # "call" or "put"
    volume: int
    open_interest: int
    volume_oi_ratio: float
    implied_volatility: float
    in_the_money: bool
    is_unusual: bool      # volume_oi_ratio > threshold


@dataclass
class UnusualOptionsData:
    """Aggregated unusual options activity result."""

    ticker: str
    total_unusual_calls: int
    total_unusual_puts: int
    total_call_volume: int
    total_put_volume: int
    put_call_ratio: float           # total_put_volume / max(1, total_call_volume)
    grade: OptionsGrade
    top_unusual: list[UnusualContract] = field(default_factory=list)
    interpretation: str = ""
    as_of_date: date = field(default_factory=date.today)
    data_available: bool = True     # False when yfinance is unreachable


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _options_grade(
    unusual_calls: int,
    unusual_puts: int,
) -> OptionsGrade:
    """Classify overall options activity grade from unusual contract counts."""
    total_unusual = unusual_calls + unusual_puts
    if total_unusual == 0:
        return "neutral"
    if unusual_calls > 2 * max(unusual_puts, 0) and unusual_calls >= 2:
        return "bullish_unusual"
    if unusual_puts > 2 * max(unusual_calls, 0) and unusual_puts >= 2:
        return "bearish_unusual"
    if total_unusual >= 3:
        return "mixed_unusual"
    return "neutral"


def _interpretation(
    grade: OptionsGrade,
    unusual_calls: int,
    unusual_puts: int,
    put_call_ratio: float,
    total_call_vol: int,
    total_put_vol: int,
    data_available: bool,
) -> str:
    if not data_available:
        return "期权数据暂时不可用（yfinance 无法获取期权链）。请稍后重试。"

    if grade == "bullish_unusual":
        return (
            f"⬆ 看涨异动！{unusual_calls} 个看涨期权合约成交量远超未平仓量（>3×）。"
            f"总 Call/Put 量：{total_call_vol:,}/{total_put_vol:,}（P/C 比 {put_call_ratio:.2f}）。"
            "集中的 call 扫单往往暗示机构在事件前做多布局，可与基本面信号联合验证。"
        )
    if grade == "bearish_unusual":
        return (
            f"⬇ 看跌异动！{unusual_puts} 个看跌期权合约成交量远超未平仓量（>3×）。"
            f"总 Call/Put 量：{total_call_vol:,}/{total_put_vol:,}（P/C 比 {put_call_ratio:.2f}）。"
            "集中的 put 扫单可能是对冲/做空布局，结合 GEX 信号判断方向。"
        )
    if grade == "mixed_unusual":
        return (
            f"↔ 双向异动（{unusual_calls} call + {unusual_puts} put 合约异常）。"
            f"P/C 比 {put_call_ratio:.2f}，市场方向分歧，可能为财报或事件前双向押注。"
        )
    return (
        f"无明显异动信号。Call/Put 量：{total_call_vol:,}/{total_put_vol:,}，"
        f"P/C 比 {put_call_ratio:.2f}。期权活动正常。"
    )


def _parse_chain(
    ticker_str: str,
    expiry: str,
    calls_df: Any,
    puts_df: Any,
) -> tuple[list[UnusualContract], list[UnusualContract]]:
    """Parse one expiry's call/put DataFrames into UnusualContract lists."""
    unusual_calls: list[UnusualContract] = []
    unusual_puts: list[UnusualContract] = []

    for option_type, df in [("call", calls_df), ("put", puts_df)]:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            continue
        for _, row in df.iterrows():
            try:
                vol = int(row.get("volume") or 0)
                oi = int(row.get("openInterest") or 0)
                ratio = round(vol / max(1, oi), 4)
                strike = float(row.get("strike") or 0)
                iv = float(row.get("impliedVolatility") or 0)
                itm = bool(row.get("inTheMoney") or False)
                is_unusual = ratio >= _VOLUME_OI_THRESHOLD and vol > 10

                contract = UnusualContract(
                    ticker=ticker_str,
                    expiry=expiry,
                    strike=strike,
                    option_type=option_type,
                    volume=vol,
                    open_interest=oi,
                    volume_oi_ratio=ratio,
                    implied_volatility=round(iv, 4),
                    in_the_money=itm,
                    is_unusual=is_unusual,
                )

                if is_unusual:
                    if option_type == "call":
                        unusual_calls.append(contract)
                    else:
                        unusual_puts.append(contract)
            except Exception:
                continue

    return unusual_calls, unusual_puts


# ---------------------------------------------------------------------------
# Main computation
# ---------------------------------------------------------------------------


def compute_unusual_options(ticker: str) -> UnusualOptionsData:
    """Scan option chains for unusual volume/OI activity.

    Always returns an UnusualOptionsData (never None, never raises).
    data_available=False when yfinance cannot fetch options.
    """
    ticker = ticker.strip().upper()

    def _degraded(reason: str) -> UnusualOptionsData:
        logger.warning(f"[UOA] {ticker}: {reason}")
        return UnusualOptionsData(
            ticker=ticker,
            total_unusual_calls=0,
            total_unusual_puts=0,
            total_call_volume=0,
            total_put_volume=0,
            put_call_ratio=1.0,
            grade="neutral",
            top_unusual=[],
            interpretation=_interpretation(
                "neutral", 0, 0, 1.0, 0, 0, False
            ),
            as_of_date=date.today(),
            data_available=False,
        )

    try:
        yt = yf.Ticker(ticker)
        expiries = getattr(yt, "options", None)
        if not expiries:
            return _degraded("no options data available")

        expiries_to_scan = list(expiries)[: _MAX_EXPIRIES]

        all_unusual_calls: list[UnusualContract] = []
        all_unusual_puts: list[UnusualContract] = []
        total_call_vol = 0
        total_put_vol = 0

        for expiry in expiries_to_scan:
            try:
                chain = yt.option_chain(expiry)
                uc, up = _parse_chain(ticker, expiry, chain.calls, chain.puts)
                all_unusual_calls.extend(uc)
                all_unusual_puts.extend(up)

                # Aggregate total volumes
                if hasattr(chain, "calls") and chain.calls is not None and not chain.calls.empty:
                    vol_series = chain.calls.get("volume")
                    if vol_series is not None:
                        total_call_vol += int(vol_series.fillna(0).sum())
                if hasattr(chain, "puts") and chain.puts is not None and not chain.puts.empty:
                    vol_series = chain.puts.get("volume")
                    if vol_series is not None:
                        total_put_vol += int(vol_series.fillna(0).sum())
            except Exception as exc:
                logger.debug(f"[UOA] {ticker} expiry {expiry}: {exc}")
                continue

        put_call_ratio = round(total_put_vol / max(1, total_call_vol), 4)
        grade = _options_grade(len(all_unusual_calls), len(all_unusual_puts))

        # Top unusual: sorted by ratio desc, cap at _TOP_N
        all_unusual = sorted(
            all_unusual_calls + all_unusual_puts,
            key=lambda c: c.volume_oi_ratio,
            reverse=True,
        )[: _TOP_N]

        interp = _interpretation(
            grade,
            len(all_unusual_calls),
            len(all_unusual_puts),
            put_call_ratio,
            total_call_vol,
            total_put_vol,
            True,
        )

        return UnusualOptionsData(
            ticker=ticker,
            total_unusual_calls=len(all_unusual_calls),
            total_unusual_puts=len(all_unusual_puts),
            total_call_volume=total_call_vol,
            total_put_volume=total_put_vol,
            put_call_ratio=put_call_ratio,
            grade=grade,
            top_unusual=all_unusual,
            interpretation=interp,
            as_of_date=date.today(),
            data_available=True,
        )

    except Exception as exc:
        logger.error(f"[UOA] {ticker} unexpected error: {exc}")
        return _degraded(f"unexpected: {exc}")

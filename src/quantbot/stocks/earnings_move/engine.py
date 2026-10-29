"""Pre-Earnings Expected Move engine.

Uses the options market's ATM straddle price to compute the implied ±% move
expected around a company's next earnings date.

Formula:
    ATM strike = strike closest to current_price
    straddle_price = ATM call last price + ATM put last price
    expected_move_pct = straddle_price / current_price × 100

This approximates the options market's implied ±1σ earnings move.

Graceful degradation: always returns EarningsMoveData (never None, never
raises). data_available=False when yfinance cannot provide the data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Literal

import yfinance as yf
from loguru import logger

EarningsMoveGrade = Literal["large_expected", "medium_expected", "small_expected", "no_data"]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class EarningsMoveData:
    """Options-implied expected move around next earnings."""

    ticker: str
    next_earnings_date: str | None         # "YYYY-MM-DD" or None
    days_to_earnings: int | None           # None if no upcoming earnings
    expected_move_pct: float | None        # ±% implied move (None if no data)
    atm_strike: float | None
    straddle_price: float | None
    current_price: float | None
    grade: EarningsMoveGrade
    interpretation: str
    as_of_date: date
    data_available: bool


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _earnings_move_grade(pct: float) -> EarningsMoveGrade:
    """Classify expected move magnitude."""
    if pct > 10.0:
        return "large_expected"
    if pct > 5.0:
        return "medium_expected"
    return "small_expected"


def _interpretation(
    grade: EarningsMoveGrade,
    pct: float | None,
    days: int | None,
    ticker: str,
    data_available: bool,
) -> str:
    if not data_available:
        return f"期权或财报数据不可用（{ticker} 可能无期权链或下次财报未定）。"

    if days is None or pct is None:
        return "无法确定下次财报日期或期权数据，请稍后重试。"

    days_str = f"{days} 天后" if days > 0 else "今天/已过"

    if grade == "large_expected":
        return (
            f"⚡ 大幅波动预警！期权市场隐含财报摆幅 ±{pct:.1f}%（{days_str}）。"
            "如此高的 implied move 意味着持有期权成本极高，期权卖方有优势；"
            "持股者应评估最坏情形是否可承受。"
        )
    if grade == "medium_expected":
        return (
            f"财报前期权隐含摆幅 ±{pct:.1f}%（{days_str}）。"
            "中等程度的预期波动，期权 premium 已部分定价财报风险。"
            f"期权买方需要超过 {pct:.1f}% 的实际波动才能盈利。"
        )
    return (
        f"财报前期权隐含摆幅 ±{pct:.1f}%（{days_str}）。"
        "预期波动较小，市场认为财报不太可能引发大波动。"
    )


def _find_nearest_expiry_after(expiries: list[str], target_date: str) -> str | None:
    """Return the first expiry on or after target_date."""
    for exp in sorted(expiries):
        if exp >= target_date:
            return exp
    return None


def _atm_straddle(
    calls_df: object,
    puts_df: object,
    current_price: float,
) -> tuple[float | None, float | None, float | None]:
    """Return (atm_strike, call_price, put_price) for nearest ATM strike."""
    try:
        import pandas as pd

        calls = calls_df if isinstance(calls_df, pd.DataFrame) else pd.DataFrame()
        puts = puts_df if isinstance(puts_df, pd.DataFrame) else pd.DataFrame()

        if calls.empty or puts.empty:
            return None, None, None

        # Find ATM strike (closest to current price)
        if "strike" not in calls.columns:
            return None, None, None

        strikes = calls["strike"].dropna().values
        if len(strikes) == 0:
            return None, None, None

        atm_strike = float(min(strikes, key=lambda s: abs(float(s) - current_price)))

        # Get ATM call and put prices
        call_row = calls[calls["strike"] == atm_strike]
        put_row = puts[puts["strike"] == atm_strike] if "strike" in puts.columns else pd.DataFrame()

        if call_row.empty or put_row.empty:
            return None, None, None

        # Prefer lastPrice, fall back to mid of bid/ask
        def _price(row: pd.DataFrame) -> float | None:
            lp = row["lastPrice"].iloc[0] if "lastPrice" in row.columns else None
            if lp is not None and float(lp) > 0:
                return float(lp)
            bid = row["bid"].iloc[0] if "bid" in row.columns else 0
            ask = row["ask"].iloc[0] if "ask" in row.columns else 0
            if ask and bid:
                return (float(bid) + float(ask)) / 2
            return None

        call_price = _price(call_row)
        put_price = _price(put_row)

        if call_price is None or put_price is None:
            return None, None, None

        return atm_strike, call_price, put_price

    except Exception as exc:
        logger.debug(f"_atm_straddle error: {exc}")
        return None, None, None


# ---------------------------------------------------------------------------
# Main computation
# ---------------------------------------------------------------------------


def compute_earnings_move(ticker: str) -> EarningsMoveData:
    """Compute options-implied expected move for next earnings.

    Always returns an EarningsMoveData (never None, never raises).
    data_available=False when yfinance cannot provide the needed data.
    """
    ticker = ticker.strip().upper()

    def _degraded(reason: str) -> EarningsMoveData:
        logger.warning(f"[EarningsMove] {ticker}: {reason}")
        return EarningsMoveData(
            ticker=ticker,
            next_earnings_date=None,
            days_to_earnings=None,
            expected_move_pct=None,
            atm_strike=None,
            straddle_price=None,
            current_price=None,
            grade="no_data",
            interpretation=_interpretation("no_data", None, None, ticker, False),
            as_of_date=date.today(),
            data_available=False,
        )

    try:
        yt = yf.Ticker(ticker)

        # ---- Current price ----
        info = yt.info or {}
        current_price: float | None = (
            info.get("regularMarketPrice")
            or info.get("currentPrice")
            or info.get("previousClose")
        )
        if not current_price:
            return _degraded("no current price from yfinance")
        current_price = float(current_price)

        # ---- Next earnings date ----
        cal = yt.calendar
        earnings_str: str | None = None

        if cal is not None:
            # cal can be a dict (old yfinance) or DataFrame (newer)
            try:
                if hasattr(cal, "get"):
                    raw = cal.get("Earnings Date")
                    if raw is not None:
                        if isinstance(raw, list) and raw:
                            earnings_str = str(raw[0])[:10]
                        else:
                            earnings_str = str(raw)[:10]
                elif hasattr(cal, "iloc"):
                    # DataFrame with Earnings Date row
                    if "Earnings Date" in cal.index:
                        val = cal.loc["Earnings Date"].iloc[0]
                        earnings_str = str(val)[:10]
            except Exception:
                pass

        if not earnings_str:
            return _degraded("no earnings date in yfinance calendar")

        today = date.today()
        try:
            earnings_dt = datetime.strptime(earnings_str, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
            earnings_date_obj = earnings_dt.date()
        except ValueError:
            return _degraded(f"cannot parse earnings date: {earnings_str!r}")

        days_to_earnings = (earnings_date_obj - today).days

        # ---- Options chain for first expiry after earnings ----
        expiries = getattr(yt, "options", None) or []
        if not expiries:
            return EarningsMoveData(
                ticker=ticker,
                next_earnings_date=earnings_str,
                days_to_earnings=days_to_earnings,
                expected_move_pct=None,
                atm_strike=None,
                straddle_price=None,
                current_price=current_price,
                grade="no_data",
                interpretation=_interpretation("no_data", None, days_to_earnings, ticker, False),
                as_of_date=today,
                data_available=False,
            )

        target_expiry = _find_nearest_expiry_after(list(expiries), earnings_str)
        if target_expiry is None:
            target_expiry = list(expiries)[0]  # fallback: nearest available

        chain = yt.option_chain(target_expiry)
        atm_strike, call_price, put_price = _atm_straddle(
            chain.calls, chain.puts, current_price
        )

        if atm_strike is None or call_price is None or put_price is None:
            return EarningsMoveData(
                ticker=ticker,
                next_earnings_date=earnings_str,
                days_to_earnings=days_to_earnings,
                expected_move_pct=None,
                atm_strike=None,
                straddle_price=None,
                current_price=current_price,
                grade="no_data",
                interpretation=_interpretation("no_data", None, days_to_earnings, ticker, False),
                as_of_date=today,
                data_available=False,
            )

        straddle = round(call_price + put_price, 4)
        move_pct = round(straddle / current_price * 100, 2)
        grade = _earnings_move_grade(move_pct)
        interp = _interpretation(grade, move_pct, days_to_earnings, ticker, True)

        return EarningsMoveData(
            ticker=ticker,
            next_earnings_date=earnings_str,
            days_to_earnings=days_to_earnings,
            expected_move_pct=move_pct,
            atm_strike=atm_strike,
            straddle_price=straddle,
            current_price=round(current_price, 4),
            grade=grade,
            interpretation=interp,
            as_of_date=today,
            data_available=True,
        )

    except Exception as exc:
        logger.error(f"[EarningsMove] {ticker} unexpected error: {exc}")
        return _degraded(f"unexpected: {exc}")

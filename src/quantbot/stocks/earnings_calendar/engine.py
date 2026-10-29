"""Earnings Calendar & Expected Move engine — Phase F.25.

在财报前用 ATM Straddle 价格估算市场隐含的预期波动，与历史实际波动对比，
给出买入/卖出 Straddle 的信号。

数据来源：yfinance 期权链 + 价格历史 + 财报日历（免费）
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import numpy as np
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 类型
# ---------------------------------------------------------------------------

StraddleSignal = Literal["buy_straddle", "sell_straddle", "fair", "unknown"]

# 信号阈值：implied/historical 比率
_SELL_RATIO = 1.5   # implied > 1.5× historical → sell (options too expensive)
_BUY_RATIO = 1.5    # historical > 1.5× implied → buy (options too cheap)
_MIN_HIST_MOVES = 2  # 至少 2 次历史财报才计算均值
_MAX_HIST_MOVES = 8  # 最多使用最近 8 次


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class EarningsMove:
    date: str
    actual_move_pct: float   # 正负值（财报后首日收益率，%）
    abs_move_pct: float       # 绝对值（%）
    beat_estimate: bool | None = None  # 是否超预期（可能缺失）


@dataclass
class EarningsCalendarData:
    ticker: str
    next_earnings_date: str | None       # ISO 格式日期
    days_to_earnings: int | None
    implied_move_pct: float | None       # ATM Straddle 成本 / spot，%
    historical_avg_move_pct: float | None  # 过去 N 次财报实际波动均值（绝对值），%
    historical_moves: list[EarningsMove] = field(default_factory=list)
    straddle_signal: StraddleSignal = "unknown"
    interpretation: str = ""
    as_of_date: str = ""
    data_available: bool = False


# ---------------------------------------------------------------------------
# 内部计算
# ---------------------------------------------------------------------------

def _get_atm_straddle_cost(
    chain_calls: pd.DataFrame,
    chain_puts: pd.DataFrame,
    spot: float,
) -> float | None:
    """估算 ATM Straddle 成本占 spot 的百分比。

    使用 lastPrice 而非 mid price，因为 yfinance 免费数据不总有 bid/ask。
    """
    if spot <= 0:
        return None

    call_price: float | None = None
    put_price: float | None = None

    if not chain_calls.empty and "strike" in chain_calls.columns and "lastPrice" in chain_calls.columns:
        idx = (chain_calls["strike"] - spot).abs().idxmin()
        p = chain_calls.loc[idx, "lastPrice"]
        v = _safe_float(p)
        if v is not None and v > 0:
            call_price = v

    if not chain_puts.empty and "strike" in chain_puts.columns and "lastPrice" in chain_puts.columns:
        idx = (chain_puts["strike"] - spot).abs().idxmin()
        p = chain_puts.loc[idx, "lastPrice"]
        v = _safe_float(p)
        if v is not None and v > 0:
            put_price = v

    if call_price is None or put_price is None:
        return None

    straddle_cost = (call_price + put_price) / spot * 100.0
    return round(straddle_cost, 2) if straddle_cost > 0 else None


def _safe_float(val: object) -> float | None:
    try:
        f = float(val)  # type: ignore[arg-type]
        return f if not math.isnan(f) else None
    except (TypeError, ValueError):
        return None


def _compute_historical_moves(
    earnings_dates: list[date],
    price_history: pd.DataFrame,
) -> list[EarningsMove]:
    """计算每次财报后的实际价格波动。

    Args:
        earnings_dates: 按时间倒序的历史财报日期列表（最近在前）
        price_history: yfinance history DataFrame，包含 'Close' 列

    Returns:
        EarningsMove 列表，最多 _MAX_HIST_MOVES 条
    """
    if price_history.empty or "Close" not in price_history.columns:
        return []

    # 确保 index 是 date-comparable
    close = price_history["Close"].dropna()
    if close.empty:
        return []

    # 构建日期 → 收盘价的映射（以交易日 date 为 key）
    close_by_date: dict[date, float] = {}
    for ts, val in close.items():
        try:
            d = ts.date() if hasattr(ts, "date") else date.fromisoformat(str(ts)[:10])
            close_by_date[d] = float(val)
        except Exception:
            continue

    sorted_dates = sorted(close_by_date.keys())

    def nearest_price(target: date, offset: int) -> float | None:
        """在 target 前后 offset 个交易日找价格（offset=-1 表示前一日）。"""
        candidates = [d for d in sorted_dates if (d - target).days * (1 if offset >= 0 else -1) >= abs(offset)]
        if not candidates:
            return None
        nearest = min(candidates, key=lambda d: abs((d - target).days))
        return close_by_date.get(nearest)

    moves: list[EarningsMove] = []
    for ed in earnings_dates[:_MAX_HIST_MOVES]:
        # 财报日前收盘
        pre_candidates = [d for d in sorted_dates if d < ed]
        post_candidates = [d for d in sorted_dates if d > ed]

        if not pre_candidates or not post_candidates:
            # 若财报日当天有交易，尝试当天 vs 前一天
            on_day_candidates = [d for d in sorted_dates if d == ed]
            if not on_day_candidates or not pre_candidates:
                continue
            pre_price = close_by_date.get(pre_candidates[-1])
            post_price = close_by_date.get(on_day_candidates[0])
        else:
            pre_price = close_by_date.get(pre_candidates[-1])
            post_price = close_by_date.get(post_candidates[0])

        if pre_price is None or post_price is None or pre_price == 0:
            continue

        move_pct = (post_price - pre_price) / pre_price * 100.0
        abs_move = abs(move_pct)

        moves.append(EarningsMove(
            date=ed.isoformat(),
            actual_move_pct=round(move_pct, 2),
            abs_move_pct=round(abs_move, 2),
        ))

    return moves


def _parse_next_earnings(tk: object) -> date | None:
    """Try various yfinance attributes to get next earnings date."""
    # Method 1: tk.calendar
    try:
        cal = tk.calendar  # type: ignore[attr-defined]
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date")
            if ed is not None:
                if isinstance(ed, (list, tuple)) and ed:
                    ed = ed[0]
                if hasattr(ed, "date") and callable(ed.date):
                    return ed.date()  # datetime → date
                if isinstance(ed, date):
                    return ed  # already a date
                if isinstance(ed, str):
                    return date.fromisoformat(ed[:10])
                return None
        if isinstance(cal, pd.DataFrame) and not cal.empty:
            row = cal.iloc[0]
            for col in row.index:
                if "earnings" in col.lower() and "date" in col.lower():
                    val = row[col]
                    if hasattr(val, "date"):
                        return val.date()
    except Exception:
        pass

    # Method 2: tk.earnings_dates
    try:
        ed_df = tk.earnings_dates  # type: ignore[attr-defined]
        if ed_df is not None and not ed_df.empty:
            today = date.today()
            for ts in ed_df.index:
                d = ts.date() if hasattr(ts, "date") else None
                if d is not None and d >= today:
                    return d
    except Exception:
        pass

    return None


def _parse_historical_earnings_dates(tk: object) -> list[date]:
    """Get historical earnings dates from yfinance, sorted newest-first."""
    today = date.today()
    dates: list[date] = []

    try:
        ed_df = tk.earnings_dates  # type: ignore[attr-defined]
        if ed_df is not None and not ed_df.empty:
            for ts in ed_df.index:
                try:
                    d = ts.date() if hasattr(ts, "date") else date.fromisoformat(str(ts)[:10])
                    if d < today:
                        dates.append(d)
                except Exception:
                    continue
    except Exception:
        pass

    dates.sort(reverse=True)
    return dates


def _build_interpretation(
    next_earnings_date: date | None,
    days_to_earnings: int | None,
    implied_move_pct: float | None,
    historical_avg_move_pct: float | None,
    straddle_signal: StraddleSignal,
) -> str:
    parts: list[str] = []

    if next_earnings_date is not None:
        parts.append(f"下次财报：{next_earnings_date.isoformat()}")
        if days_to_earnings is not None:
            parts.append(f"（{days_to_earnings} 天后）")
    else:
        parts.append("未检测到待发布财报")

    if implied_move_pct is not None:
        parts.append(f"期权隐含波动 ±{implied_move_pct:.1f}%")
    if historical_avg_move_pct is not None:
        parts.append(f"历史平均实际波动 ±{historical_avg_move_pct:.1f}%")

    if straddle_signal == "sell_straddle":
        parts.append("期权定价偏贵（隐含 > 1.5× 历史均值），可考虑卖出 Straddle 或 Iron Condor")
    elif straddle_signal == "buy_straddle":
        parts.append("期权定价偏便宜（历史均值 > 1.5× 隐含），可考虑买入 Straddle")
    elif straddle_signal == "fair":
        parts.append("期权定价合理，观望为主")

    return "；".join(parts) + "。" if parts else "数据不可用。"


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------

def compute_earnings_calendar(ticker: str) -> EarningsCalendarData:
    """计算财报日期与期权预期波动。永不 raise，失败时 data_available=False。"""
    today_str = date.today().isoformat()
    today = date.today()

    _default = EarningsCalendarData(
        ticker=ticker.upper(),
        next_earnings_date=None,
        days_to_earnings=None,
        implied_move_pct=None,
        historical_avg_move_pct=None,
        historical_moves=[],
        straddle_signal="unknown",
        interpretation="数据不可用。",
        as_of_date=today_str,
        data_available=False,
    )

    try:
        tk = yf.Ticker(ticker)

        # ── 价格历史（2年，用于历史财报波动计算）────────────────────────
        hist = tk.history(period="2y")
        if hist is None or hist.empty:
            return _default

        # ── 当前价格 ─────────────────────────────────────────────────────
        try:
            info = tk.info
            spot = (
                _safe_float(info.get("regularMarketPrice"))
                or _safe_float(info.get("currentPrice"))
            )
        except Exception:
            spot = None

        if spot is None or spot <= 0:
            spot_s = hist["Close"].dropna()
            spot = float(spot_s.iloc[-1]) if not spot_s.empty else None
        if spot is None or spot <= 0:
            return _default

        # ── 下次财报日 ────────────────────────────────────────────────────
        next_ed = _parse_next_earnings(tk)
        days_to = (next_ed - today).days if next_ed is not None else None

        # ── 历史财报波动 ──────────────────────────────────────────────────
        hist_dates = _parse_historical_earnings_dates(tk)
        hist_moves = _compute_historical_moves(hist_dates, hist)
        hist_abs = [m.abs_move_pct for m in hist_moves]
        historical_avg = (
            round(float(np.mean(hist_abs)), 2)
            if len(hist_abs) >= _MIN_HIST_MOVES
            else None
        )

        # ── ATM Straddle 成本（找财报后最近期权到期日）───────────────────
        implied_move: float | None = None
        try:
            expiry_dates = tk.options
            if expiry_dates and next_ed is not None:
                # 找财报日当天或之后的首个期权到期日
                target_expiry: str | None = None
                for exp_str in expiry_dates:
                    try:
                        exp_date = date.fromisoformat(exp_str)
                    except ValueError:
                        continue
                    if exp_date >= next_ed:
                        target_expiry = exp_str
                        break

                if target_expiry is None and expiry_dates:
                    # fallback: 最近到期日
                    for exp_str in expiry_dates:
                        try:
                            exp_date = date.fromisoformat(exp_str)
                            if exp_date > today:
                                target_expiry = exp_str
                                break
                        except ValueError:
                            continue

                if target_expiry:
                    chain = tk.option_chain(target_expiry)
                    implied_move = _get_atm_straddle_cost(chain.calls, chain.puts, spot)
            elif expiry_dates and next_ed is None:
                # No upcoming earnings but still try to get ATM straddle from nearest expiry
                for exp_str in expiry_dates:
                    try:
                        exp_date = date.fromisoformat(exp_str)
                        if exp_date > today:
                            chain = tk.option_chain(exp_str)
                            implied_move = _get_atm_straddle_cost(chain.calls, chain.puts, spot)
                            break
                    except Exception:
                        continue
        except Exception as e:
            logger.warning(f"[EarningsCalendar] options fetch failed for {ticker}: {e}")

        # ── 信号 ─────────────────────────────────────────────────────────
        signal: StraddleSignal = "unknown"
        if implied_move is not None and historical_avg is not None and historical_avg > 0:
            ratio = implied_move / historical_avg
            if ratio >= _SELL_RATIO:
                signal = "sell_straddle"
            elif (1.0 / ratio) >= _BUY_RATIO:
                signal = "buy_straddle"
            else:
                signal = "fair"

        # ── 解读 ─────────────────────────────────────────────────────────
        interpretation = _build_interpretation(
            next_earnings_date=next_ed,
            days_to_earnings=days_to,
            implied_move_pct=implied_move,
            historical_avg_move_pct=historical_avg,
            straddle_signal=signal,
        )

        return EarningsCalendarData(
            ticker=ticker.upper(),
            next_earnings_date=next_ed.isoformat() if next_ed else None,
            days_to_earnings=days_to,
            implied_move_pct=implied_move,
            historical_avg_move_pct=historical_avg,
            historical_moves=hist_moves,
            straddle_signal=signal,
            interpretation=interpretation,
            as_of_date=today_str,
            data_available=True,
        )

    except Exception as e:
        logger.warning(f"[EarningsCalendar] Error for {ticker}: {e}")
        return _default

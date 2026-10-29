"""Options Max Pain Calculator engine — Phase F.29.

Max Pain Strike = 期权到期时，所有期权持有者总损失最大的标的价格。
做市商倾向于在到期日前将标的价格"钉"在 Max Pain 附近。

策略：
- 当前价格 < Max Pain → 价格有上拉力（bullish_pull）
- 当前价格 > Max Pain → 价格有下压力（bearish_pull）
- 距离 < 2% → Pin Zone（钉子区，价格可能被钉住）

数据来源：yfinance 期权链（免费）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 类型
# ---------------------------------------------------------------------------

MaxPainSignal = Literal[
    "pin_zone",       # |distance| < 2% → price likely to be pinned
    "bullish_pull",   # price < max_pain, 2-5% → upward magnetic pull
    "bearish_pull",   # price > max_pain, 2-5% → downward magnetic pull
    "weak_pull",      # |distance| >= 5% → weak magnet effect
    "unknown",
]

# 信号阈值
_PIN_ZONE_PCT = 2.0    # |distance| < 2% → pin zone
_NEAR_PIN_PCT = 5.0    # 2-5% → directional pull
_MAX_EXPIRIES = 4      # 最多处理 4 个到期日
_MIN_DTE = 1           # 跳过已过期
_MAX_DTE = 45          # 只看近期到期日


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class ExpiryMaxPain:
    expiry: str                     # YYYY-MM-DD
    dte: int                        # days to expiry
    max_pain_strike: float          # 最大痛苦值价格
    current_price: float            # 标的当前价格
    distance_pct: float             # (max_pain - current) / current × 100
    total_call_oi: int              # 该到期日总 Call OI
    total_put_oi: int               # 该到期日总 Put OI
    signal: MaxPainSignal


@dataclass
class MaxPainData:
    ticker: str
    current_price: float | None
    expiries: list[ExpiryMaxPain] = field(default_factory=list)
    interpretation: str = ""
    as_of_date: str = ""
    data_available: bool = False


# ---------------------------------------------------------------------------
# 内部计算
# ---------------------------------------------------------------------------

def _compute_max_pain(
    calls: pd.DataFrame,
    puts: pd.DataFrame,
) -> tuple[float | None, int, int]:
    """计算 Max Pain Strike。

    Returns:
        (max_pain_strike_or_None, total_call_oi, total_put_oi)

    算法：
        对每个候选 spot（= 每个 strike），计算如果价格在该 spot 到期，
        所有买方的总损失 = Σ call_OI * max(0, spot-strike) + put_OI * max(0, strike-spot)
        Max Pain = 使总损失最大（买方）/ 使 Maker 损失最小的 spot。
    """
    if calls is None or puts is None:
        return None, 0, 0

    # 清理 OI 数据
    def _clean(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        if "openInterest" not in df.columns:
            return pd.DataFrame()
        df2 = df[["strike", "openInterest"]].copy()
        df2["openInterest"] = pd.to_numeric(df2["openInterest"], errors="coerce").fillna(0).astype(int)
        df2 = df2[df2["openInterest"] > 0]
        return df2

    calls_clean = _clean(calls)
    puts_clean = _clean(puts)

    total_call_oi = int(calls_clean["openInterest"].sum()) if not calls_clean.empty else 0
    total_put_oi = int(puts_clean["openInterest"].sum()) if not puts_clean.empty else 0

    if calls_clean.empty and puts_clean.empty:
        return None, total_call_oi, total_put_oi

    # Union of all strikes as candidate spots
    all_strikes: set[float] = set()
    if not calls_clean.empty:
        all_strikes |= set(calls_clean["strike"].tolist())
    if not puts_clean.empty:
        all_strikes |= set(puts_clean["strike"].tolist())

    if not all_strikes:
        return None, total_call_oi, total_put_oi

    # For each candidate spot, compute total buyer loss
    min_loss = float("inf")
    max_pain_strike: float | None = None

    calls_dict = dict(zip(calls_clean["strike"], calls_clean["openInterest"])) if not calls_clean.empty else {}
    puts_dict = dict(zip(puts_clean["strike"], puts_clean["openInterest"])) if not puts_clean.empty else {}

    for spot in sorted(all_strikes):
        # Call holders lose nothing if spot < strike; lose (spot-strike)*oi if spot > strike
        call_loss = sum(oi * max(0.0, spot - k) for k, oi in calls_dict.items())
        # Put holders lose nothing if spot > strike; lose (strike-spot)*oi if spot < strike
        put_loss = sum(oi * max(0.0, k - spot) for k, oi in puts_dict.items())
        total_loss = call_loss + put_loss
        if total_loss < min_loss:
            min_loss = total_loss
            max_pain_strike = spot

    return max_pain_strike, total_call_oi, total_put_oi


def _classify_signal(distance_pct: float) -> MaxPainSignal:
    """Classify max pain signal based on distance percentage."""
    abs_dist = abs(distance_pct)
    if abs_dist < _PIN_ZONE_PCT:
        return "pin_zone"
    if abs_dist < _NEAR_PIN_PCT:
        return "bullish_pull" if distance_pct > 0 else "bearish_pull"
    return "weak_pull"


def _get_dte(expiry_str: str) -> int:
    """Compute days to expiry from YYYY-MM-DD string."""
    try:
        exp = date.fromisoformat(expiry_str)
        return (exp - date.today()).days
    except Exception:
        return -1


def _build_interpretation(
    ticker: str,
    current_price: float | None,
    expiries: list[ExpiryMaxPain],
) -> str:
    if not expiries:
        return f"{ticker} 无近期期权数据（DTE 1-45）。"
    parts: list[str] = []
    if current_price is not None:
        parts.append(f"当前价格 ${current_price:.2f}")
    for ep in expiries[:2]:  # show top 2
        dist_str = f"{ep.distance_pct:+.1f}%"
        signal_map = {
            "pin_zone": "⚡ 钉子区",
            "bullish_pull": "↑ 上拉",
            "bearish_pull": "↓ 下压",
            "weak_pull": "弱磁力",
            "unknown": "—",
        }
        parts.append(
            f"{ep.expiry}(DTE {ep.dte}) Max Pain ${ep.max_pain_strike:.1f}（距离{dist_str}，{signal_map.get(ep.signal, '')}）"
        )
    return "；".join(parts) + "。"


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------

def compute_max_pain(ticker: str) -> MaxPainData:
    """计算 Max Pain 数据。永不 raise。yfinance 失败时 data_available=False。"""
    today_str = date.today().isoformat()

    _default = MaxPainData(
        ticker=ticker,
        current_price=None,
        expiries=[],
        interpretation="数据不可用。",
        as_of_date=today_str,
        data_available=False,
    )

    try:
        tk = yf.Ticker(ticker)
        info: dict = tk.info or {}
        option_dates: tuple = tk.options
    except Exception as e:
        logger.warning(f"[MaxPain] yfinance fetch failed for {ticker}: {e}")
        return _default

    # Current price
    current_price: float | None = None
    for key in ("currentPrice", "regularMarketPrice", "previousClose"):
        v = info.get(key)
        if v is not None:
            try:
                current_price = float(v)
                break
            except Exception:
                pass

    if not option_dates:
        interp = f"{ticker} 无期权数据。"
        return MaxPainData(
            ticker=ticker,
            current_price=current_price,
            expiries=[],
            interpretation=interp,
            as_of_date=today_str,
            data_available=True,
        )

    expiries: list[ExpiryMaxPain] = []
    count = 0

    for expiry_str in option_dates:
        if count >= _MAX_EXPIRIES:
            break
        dte = _get_dte(expiry_str)
        if dte < _MIN_DTE or dte > _MAX_DTE:
            continue

        try:
            chain = tk.option_chain(expiry_str)
            calls = chain.calls
            puts = chain.puts
        except Exception as e:
            logger.warning(f"[MaxPain] chain fetch failed for {ticker} {expiry_str}: {e}")
            continue

        max_pain, call_oi, put_oi = _compute_max_pain(calls, puts)
        if max_pain is None:
            continue

        if current_price is not None and current_price > 0:
            dist_pct = (max_pain - current_price) / current_price * 100.0
            signal = _classify_signal(dist_pct)
            ref_price = current_price
        else:
            dist_pct = 0.0
            signal = "unknown"
            ref_price = max_pain

        expiries.append(ExpiryMaxPain(
            expiry=expiry_str,
            dte=dte,
            max_pain_strike=round(max_pain, 2),
            current_price=round(ref_price, 2),
            distance_pct=round(dist_pct, 2),
            total_call_oi=call_oi,
            total_put_oi=put_oi,
            signal=signal,
        ))
        count += 1

    # Sort by DTE
    expiries.sort(key=lambda x: x.dte)

    interpretation = _build_interpretation(ticker, current_price, expiries)

    return MaxPainData(
        ticker=ticker,
        current_price=round(current_price, 2) if current_price is not None else None,
        expiries=expiries,
        interpretation=interpretation,
        as_of_date=today_str,
        data_available=True,
    )

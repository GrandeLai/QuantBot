"""Technical Momentum Score engine — Phase F.30.

计算综合技术动量评分：RSI、MACD、布林带位置、成交量比、52 周位置。
多信号加权综合，给出 -100 到 +100 的评分和最终信号。

数据来源：yfinance 历史价格（免费）
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date
from typing import Literal

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 类型
# ---------------------------------------------------------------------------

TechSignal = Literal[
    "strong_buy",
    "buy",
    "neutral",
    "sell",
    "strong_sell",
    "no_data",
]

# 综合评分阈值
_STRONG_BUY = 60
_BUY = 30
_SELL = -30
_STRONG_SELL = -60

# RSI 阈值
_RSI_OVERSOLD = 30.0
_RSI_OVERBOUGHT = 70.0

# BB 阈值
_BB_LOW = 20.0
_BB_HIGH = 80.0

# 52 周阈值
_W52_LOW = 30.0
_W52_HIGH = 70.0

# Volume ratio
_VOL_HIGH = 2.0

# 最少需要数据天数
_MIN_BARS = 30


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class TechnicalScoreData:
    ticker: str
    current_price: float | None        # 最新收盘价
    rsi14: float | None                # RSI(14)，0-100
    macd_line: float | None            # MACD 线
    macd_signal: float | None          # MACD 信号线
    macd_histogram: float | None       # MACD Histogram
    bb_position: float | None          # 布林带位置，0-100%
    volume_ratio: float | None         # 成交量比（当前/20日均量）
    week52_position: float | None      # 52 周区间位置，0-100%
    composite_score: float | None      # 综合评分，-100 到 +100
    signal: TechSignal                 # 最终信号
    interpretation: str
    as_of_date: str
    data_available: bool


# ---------------------------------------------------------------------------
# 内部计算
# ---------------------------------------------------------------------------

def _compute_rsi(close: pd.Series, period: int = 14) -> float | None:
    """Wilder RSI，返回最新 RSI 值（0-100）。"""
    if len(close) < period + 1:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    last_loss = avg_loss.iloc[-1]
    last_gain = avg_gain.iloc[-1]
    if last_loss == 0:
        return 100.0
    rs = last_gain / last_loss
    return round(float(100.0 - 100.0 / (1.0 + rs)), 2)


def _compute_macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[float | None, float | None, float | None]:
    """MACD = EMA_fast - EMA_slow; Signal = EMA9(MACD); Histogram = MACD - Signal.

    Returns (macd_line, signal_line, histogram) or (None, None, None).
    """
    if len(close) < slow + signal:
        return None, None, None
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    sig = macd.ewm(span=signal, adjust=False).mean()
    hist = macd - sig
    return (
        round(float(macd.iloc[-1]), 6),
        round(float(sig.iloc[-1]), 6),
        round(float(hist.iloc[-1]), 6),
    )


def _compute_bb(close: pd.Series, period: int = 20, n_std: float = 2.0) -> float | None:
    """布林带位置：(close - lower) / (upper - lower) × 100.

    Returns 0-100 or None.
    """
    if len(close) < period:
        return None
    sma = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    upper = sma + n_std * std
    lower = sma - n_std * std
    last_close = float(close.iloc[-1])
    last_upper = float(upper.iloc[-1])
    last_lower = float(lower.iloc[-1])
    band_width = last_upper - last_lower
    if band_width <= 0:
        return None
    pos = (last_close - last_lower) / band_width * 100.0
    return round(max(0.0, min(100.0, pos)), 2)


def _compute_volume_ratio(volume: pd.Series, period: int = 20) -> float | None:
    """Current volume / 20-day average volume."""
    if len(volume) < period + 1:
        return None
    avg = float(volume.iloc[-period - 1:-1].mean())
    if avg <= 0:
        return None
    return round(float(volume.iloc[-1]) / avg, 4)


def _compute_52w_position(close: pd.Series, period: int = 252) -> float | None:
    """(close - 52w_low) / (52w_high - 52w_low) × 100."""
    window = close.iloc[-period:]
    if len(window) < 5:
        return None
    low52 = float(window.min())
    high52 = float(window.max())
    rng = high52 - low52
    if rng <= 0:
        return None
    pos = (float(close.iloc[-1]) - low52) / rng * 100.0
    return round(max(0.0, min(100.0, pos)), 2)


def _compute_composite_score(
    rsi: float | None,
    macd_hist: float | None,
    bb_pos: float | None,
    vol_ratio: float | None,
    close_up: bool | None,  # whether latest close > previous close
    w52_pos: float | None,
) -> float | None:
    """Compute composite score from -100 to +100.

    Each component contributes ±20 points (RSI, MACD, BB, Volume, 52w).
    """
    score = 0.0
    components = 0

    # RSI component (±20 points)
    if rsi is not None:
        if rsi < _RSI_OVERSOLD:
            score += 20.0
        elif rsi > _RSI_OVERBOUGHT:
            score -= 20.0
        else:
            # Linear interpolation: 30→+20, 50→0, 70→-20
            score += (50.0 - rsi) / 20.0 * 20.0
        components += 1

    # MACD histogram component (±20 points)
    if macd_hist is not None:
        if macd_hist > 0:
            score += 20.0
        elif macd_hist < 0:
            score -= 20.0
        components += 1

    # Bollinger Band position (±20 points)
    if bb_pos is not None:
        if bb_pos < _BB_LOW:
            score += 20.0
        elif bb_pos > _BB_HIGH:
            score -= 20.0
        components += 1

    # Volume ratio component (±10 points, volume confirmation)
    if vol_ratio is not None and close_up is not None:
        if vol_ratio > _VOL_HIGH:
            score += 10.0 if close_up else -10.0
        components += 1

    # 52-week position (±10 points, contrarian)
    if w52_pos is not None:
        if w52_pos < _W52_LOW:
            score += 10.0   # near 52w low → mean reversion opportunity
        elif w52_pos > _W52_HIGH:
            score -= 10.0   # near 52w high → potential exhaustion
        components += 1

    if components == 0:
        return None

    return round(score, 2)


def _classify_signal(score: float | None) -> TechSignal:
    """Classify composite score into signal."""
    if score is None:
        return "no_data"
    if score >= _STRONG_BUY:
        return "strong_buy"
    if score >= _BUY:
        return "buy"
    if score <= _STRONG_SELL:
        return "strong_sell"
    if score <= _SELL:
        return "sell"
    return "neutral"


def _build_interpretation(
    ticker: str,
    rsi: float | None,
    macd_hist: float | None,
    bb_pos: float | None,
    vol_ratio: float | None,
    w52_pos: float | None,
    score: float | None,
    signal: TechSignal,
) -> str:
    parts: list[str] = []

    signal_map = {
        "strong_buy": "强烈买入",
        "buy": "买入",
        "neutral": "中性",
        "sell": "卖出",
        "strong_sell": "强烈卖出",
        "no_data": "数据不足",
    }
    if score is not None:
        parts.append(f"综合评分 {score:+.0f}（{signal_map.get(signal, '')}）")

    if rsi is not None:
        rsi_note = "超买" if rsi > _RSI_OVERBOUGHT else ("超卖" if rsi < _RSI_OVERSOLD else "正常")
        parts.append(f"RSI {rsi:.1f}（{rsi_note}）")

    if macd_hist is not None:
        parts.append(f"MACD{'↑' if macd_hist > 0 else '↓'} Histogram {macd_hist:+.4f}")

    if bb_pos is not None:
        bb_note = "超卖区" if bb_pos < _BB_LOW else ("超买区" if bb_pos > _BB_HIGH else "带内")
        parts.append(f"BB位置 {bb_pos:.1f}%（{bb_note}）")

    if vol_ratio is not None:
        parts.append(f"成交量 {vol_ratio:.2f}×均量")

    if w52_pos is not None:
        parts.append(f"52w分位 {w52_pos:.1f}%")

    return "；".join(parts) + "。" if parts else "数据不足。"


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------

def compute_technical_score(ticker: str) -> TechnicalScoreData:
    """计算技术动量评分。永不 raise。yfinance 失败时 data_available=False。"""
    today_str = date.today().isoformat()

    _default = TechnicalScoreData(
        ticker=ticker,
        current_price=None,
        rsi14=None,
        macd_line=None,
        macd_signal=None,
        macd_histogram=None,
        bb_position=None,
        volume_ratio=None,
        week52_position=None,
        composite_score=None,
        signal="no_data",
        interpretation="数据不可用。",
        as_of_date=today_str,
        data_available=False,
    )

    try:
        tk = yf.Ticker(ticker)
        hist = tk.history(period="1y")
    except Exception as e:
        logger.warning(f"[TechScore] yfinance fetch failed for {ticker}: {e}")
        return _default

    if hist is None or hist.empty or "Close" not in hist.columns:
        return TechnicalScoreData(
            ticker=ticker,
            current_price=None,
            rsi14=None,
            macd_line=None,
            macd_signal=None,
            macd_histogram=None,
            bb_position=None,
            volume_ratio=None,
            week52_position=None,
            composite_score=None,
            signal="no_data",
            interpretation="无历史价格数据。",
            as_of_date=today_str,
            data_available=True,
        )

    close = hist["Close"].dropna()
    volume = hist["Volume"].dropna() if "Volume" in hist.columns else pd.Series(dtype=float)

    if len(close) < _MIN_BARS:
        return TechnicalScoreData(
            ticker=ticker,
            current_price=round(float(close.iloc[-1]), 2) if len(close) > 0 else None,
            rsi14=None,
            macd_line=None,
            macd_signal=None,
            macd_histogram=None,
            bb_position=None,
            volume_ratio=None,
            week52_position=None,
            composite_score=None,
            signal="no_data",
            interpretation=f"历史数据不足（仅 {len(close)} 日，需 ≥ {_MIN_BARS}）。",
            as_of_date=today_str,
            data_available=True,
        )

    current_price = round(float(close.iloc[-1]), 4)
    prev_close = float(close.iloc[-2]) if len(close) > 1 else None
    close_up: bool | None = (current_price > prev_close) if prev_close is not None else None

    rsi = _compute_rsi(close)
    macd_line, macd_sig_line, macd_hist = _compute_macd(close)
    bb_pos = _compute_bb(close)
    vol_ratio = _compute_volume_ratio(volume) if not volume.empty else None
    w52_pos = _compute_52w_position(close)

    score = _compute_composite_score(
        rsi=rsi,
        macd_hist=macd_hist,
        bb_pos=bb_pos,
        vol_ratio=vol_ratio,
        close_up=close_up,
        w52_pos=w52_pos,
    )
    signal = _classify_signal(score)

    interpretation = _build_interpretation(
        ticker=ticker,
        rsi=rsi,
        macd_hist=macd_hist,
        bb_pos=bb_pos,
        vol_ratio=vol_ratio,
        w52_pos=w52_pos,
        score=score,
        signal=signal,
    )

    def _safe(v: float | None, decimals: int = 4) -> float | None:
        return round(v, decimals) if v is not None and not math.isnan(v) else None

    return TechnicalScoreData(
        ticker=ticker,
        current_price=current_price,
        rsi14=_safe(rsi, 2),
        macd_line=_safe(macd_line, 6),
        macd_signal=_safe(macd_sig_line, 6),
        macd_histogram=_safe(macd_hist, 6),
        bb_position=_safe(bb_pos, 2),
        volume_ratio=_safe(vol_ratio, 4),
        week52_position=_safe(w52_pos, 2),
        composite_score=_safe(score, 2),
        signal=signal,
        interpretation=interpretation,
        as_of_date=today_str,
        data_available=True,
    )

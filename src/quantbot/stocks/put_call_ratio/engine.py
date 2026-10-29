"""Put/Call Ratio & Options Sentiment engine — Phase F.26.

聚合期权链中所有到期日的 Put/Call 成交量和持仓量，计算 PCR 情绪指标。
PCR 极值是经典的反向情绪信号。

数据来源：yfinance 期权链（免费）
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 类型
# ---------------------------------------------------------------------------

PCRSentiment = Literal[
    "extreme_bearish",
    "bearish",
    "neutral",
    "bullish",
    "extreme_bullish",
    "unknown",
]

# Volume PCR 情绪阈值
_EXTREME_BEARISH = 1.5
_BEARISH = 1.0
_BULLISH = 0.7
_EXTREME_BULLISH = 0.5

_MAX_EXPIRIES = 8   # 最多聚合的到期日数


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class ExpiryPCR:
    expiry: str
    days_to_expiry: int
    call_volume: int
    put_volume: int
    volume_pcr: float | None    # put_volume / call_volume
    call_oi: int
    put_oi: int
    oi_pcr: float | None        # put_oi / call_oi


@dataclass
class PCRData:
    ticker: str
    volume_pcr: float | None        # 全链汇总 PCR（成交量）
    oi_pcr: float | None            # 全链汇总 PCR（持仓量）
    total_call_volume: int
    total_put_volume: int
    total_call_oi: int
    total_put_oi: int
    expiry_breakdown: list[ExpiryPCR] = field(default_factory=list)
    sentiment: PCRSentiment = "unknown"
    interpretation: str = ""
    as_of_date: str = ""
    data_available: bool = False


# ---------------------------------------------------------------------------
# 内部计算
# ---------------------------------------------------------------------------

def _safe_int(val: object) -> int:
    """Convert to int, default 0 on failure."""
    try:
        v = float(val)  # type: ignore[arg-type]
        return int(v) if not math.isnan(v) else 0
    except (TypeError, ValueError):
        return 0


def _compute_pcr(put_val: int | float, call_val: int | float) -> float | None:
    """Safely compute put/call ratio. Returns None if call_val == 0."""
    if call_val <= 0:
        return None
    return round(put_val / call_val, 4)


def _classify_sentiment(volume_pcr: float | None) -> PCRSentiment:
    if volume_pcr is None:
        return "unknown"
    if volume_pcr >= _EXTREME_BEARISH:
        return "extreme_bearish"
    if volume_pcr >= _BEARISH:
        return "bearish"
    if volume_pcr >= _BULLISH:
        return "neutral"
    if volume_pcr >= _EXTREME_BULLISH:
        return "bullish"
    return "extreme_bullish"


def _aggregate_chain(chain_calls: pd.DataFrame, chain_puts: pd.DataFrame) -> tuple[int, int, int, int]:
    """Return (call_vol, put_vol, call_oi, put_oi) for one expiry."""
    def _col_sum(df: pd.DataFrame, col: str) -> int:
        if df.empty or col not in df.columns:
            return 0
        return sum(_safe_int(v) for v in df[col])

    call_vol = _col_sum(chain_calls, "volume")
    put_vol = _col_sum(chain_puts, "volume")
    call_oi = _col_sum(chain_calls, "openInterest")
    put_oi = _col_sum(chain_puts, "openInterest")
    return call_vol, put_vol, call_oi, put_oi


def _build_interpretation(
    volume_pcr: float | None,
    oi_pcr: float | None,
    sentiment: PCRSentiment,
    total_call_volume: int,
    total_put_volume: int,
) -> str:
    parts: list[str] = []

    if volume_pcr is not None:
        parts.append(f"Volume PCR {volume_pcr:.2f}")
    if oi_pcr is not None:
        parts.append(f"OI PCR {oi_pcr:.2f}")
    if total_call_volume > 0 or total_put_volume > 0:
        parts.append(f"Call量 {total_call_volume:,} / Put量 {total_put_volume:,}")

    sentiment_map: dict[PCRSentiment, str] = {
        "extreme_bearish": "市场极度恐慌（PCR ≥ 1.5），反向偏多信号",
        "bearish": "市场偏悲观（PCR 1.0-1.5），期权偏向保护性买 Put",
        "neutral": "市场情绪中性（PCR 0.7-1.0）",
        "bullish": "市场偏乐观（PCR 0.5-0.7），Call 占主导",
        "extreme_bullish": "市场极度贪婪（PCR < 0.5），反向偏空信号",
        "unknown": "PCR 数据不可用",
    }
    parts.append(sentiment_map.get(sentiment, ""))

    return "；".join(p for p in parts if p) + "。" if parts else "数据不可用。"


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------

def compute_put_call_ratio(ticker: str) -> PCRData:
    """计算 Put/Call Ratio 及期权情绪。永不 raise，失败时 data_available=False。"""
    today_str = date.today().isoformat()
    today = date.today()

    _default = PCRData(
        ticker=ticker.upper(),
        volume_pcr=None,
        oi_pcr=None,
        total_call_volume=0,
        total_put_volume=0,
        total_call_oi=0,
        total_put_oi=0,
        expiry_breakdown=[],
        sentiment="unknown",
        interpretation="数据不可用。",
        as_of_date=today_str,
        data_available=False,
    )

    try:
        tk = yf.Ticker(ticker)

        expiry_dates = tk.options
        if not expiry_dates:
            # No options listed — return with data_available=True but unknown
            return PCRData(
                ticker=ticker.upper(),
                volume_pcr=None,
                oi_pcr=None,
                total_call_volume=0,
                total_put_volume=0,
                total_call_oi=0,
                total_put_oi=0,
                expiry_breakdown=[],
                sentiment="unknown",
                interpretation="该标的无上市期权数据。",
                as_of_date=today_str,
                data_available=True,
            )

        # ── 聚合各到期日期权链 ─────────────────────────────────────────────
        total_call_vol = 0
        total_put_vol = 0
        total_call_oi = 0
        total_put_oi = 0
        breakdown: list[ExpiryPCR] = []

        processed = 0
        for exp_str in expiry_dates:
            if processed >= _MAX_EXPIRIES:
                break
            try:
                exp_date = date.fromisoformat(exp_str)
            except ValueError:
                continue
            if exp_date <= today:
                continue  # 跳过已过期

            dte = (exp_date - today).days
            try:
                chain = tk.option_chain(exp_str)
                cv, pv, co, po = _aggregate_chain(chain.calls, chain.puts)
            except Exception as e:
                logger.warning(f"[PCR] option_chain failed for {ticker} {exp_str}: {e}")
                continue

            total_call_vol += cv
            total_put_vol += pv
            total_call_oi += co
            total_put_oi += po

            breakdown.append(ExpiryPCR(
                expiry=exp_str,
                days_to_expiry=dte,
                call_volume=cv,
                put_volume=pv,
                volume_pcr=_compute_pcr(pv, cv),
                call_oi=co,
                put_oi=po,
                oi_pcr=_compute_pcr(po, co),
            ))
            processed += 1

        # ── 汇总 PCR ─────────────────────────────────────────────────────
        volume_pcr = _compute_pcr(total_put_vol, total_call_vol)
        oi_pcr = _compute_pcr(total_put_oi, total_call_oi)

        # ── 情绪 ─────────────────────────────────────────────────────────
        sentiment = _classify_sentiment(volume_pcr)

        # ── 解读 ─────────────────────────────────────────────────────────
        interpretation = _build_interpretation(
            volume_pcr=volume_pcr,
            oi_pcr=oi_pcr,
            sentiment=sentiment,
            total_call_volume=total_call_vol,
            total_put_volume=total_put_vol,
        )

        return PCRData(
            ticker=ticker.upper(),
            volume_pcr=volume_pcr,
            oi_pcr=oi_pcr,
            total_call_volume=total_call_vol,
            total_put_volume=total_put_vol,
            total_call_oi=total_call_oi,
            total_put_oi=total_put_oi,
            expiry_breakdown=breakdown,
            sentiment=sentiment,
            interpretation=interpretation,
            as_of_date=today_str,
            data_available=True,
        )

    except Exception as e:
        logger.warning(f"[PCR] Error for {ticker}: {e}")
        return _default

"""IV Rank & Volatility Monitor engine — Phase F.24.

计算期权隐含波动率排名（IV Rank / IV Percentile）、历史波动率（HV10/20/30/60）、
Put/Call Skew 以及期权期限结构。

数据来源：yfinance 期权链 + 价格历史（免费）
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

IVSignal = Literal["buy_options", "sell_options", "neutral", "no_data"]

# IV Rank 信号阈值
_BUY_THRESHOLD = 20.0   # IV Rank < 20 → 期权偏便宜
_SELL_THRESHOLD = 80.0  # IV Rank > 80 → 期权偏贵
_MIN_DTE = 5            # 跳过 DTE < 5 的到期日（避免 expiry pinning 噪音）
_MAX_EXPIRIES = 5       # 最多处理 5 个到期日


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class TermStructurePoint:
    expiry: str
    days_to_expiry: int
    atm_iv: float  # 百分比，如 25.5 表示 25.5%


@dataclass
class IVRankData:
    ticker: str
    current_iv: float | None        # ATM IV，百分比
    iv_rank: float | None           # 0-100
    iv_percentile: float | None     # 0-100
    hv10: float | None              # 10 日历史波动率，百分比
    hv20: float | None
    hv30: float | None
    hv60: float | None
    put_call_skew: float | None     # put IV − call IV，百分比点差
    term_structure: list[TermStructurePoint] = field(default_factory=list)
    iv_signal: IVSignal = "no_data"
    interpretation: str = ""
    as_of_date: str = ""
    data_available: bool = False


# ---------------------------------------------------------------------------
# 内部计算
# ---------------------------------------------------------------------------

def _compute_hv(prices: pd.Series, window: int) -> pd.Series:
    """计算滚动历史波动率（年化）。"""
    log_returns = np.log(prices / prices.shift(1))
    hv = log_returns.rolling(window).std() * math.sqrt(252)
    return hv


def _get_atm_iv(
    chain_calls: pd.DataFrame,
    chain_puts: pd.DataFrame,
    spot: float,
) -> tuple[float | None, float | None, float | None]:
    """从期权链获取 ATM call IV、put IV、及其均值。

    Returns:
        (call_iv, put_iv, atm_avg_iv) — 原始小数（非百分比）
    """
    call_iv: float | None = None
    put_iv: float | None = None

    if not chain_calls.empty and "strike" in chain_calls.columns and "impliedVolatility" in chain_calls.columns:
        closest_idx = (chain_calls["strike"] - spot).abs().idxmin()
        iv = chain_calls.loc[closest_idx, "impliedVolatility"]
        if iv is not None and not math.isnan(float(iv)) and float(iv) > 0:
            call_iv = float(iv)

    if not chain_puts.empty and "strike" in chain_puts.columns and "impliedVolatility" in chain_puts.columns:
        closest_idx = (chain_puts["strike"] - spot).abs().idxmin()
        iv = chain_puts.loc[closest_idx, "impliedVolatility"]
        if iv is not None and not math.isnan(float(iv)) and float(iv) > 0:
            put_iv = float(iv)

    if call_iv is not None and put_iv is not None:
        atm_iv = (call_iv + put_iv) / 2.0
    elif call_iv is not None:
        atm_iv = call_iv
    elif put_iv is not None:
        atm_iv = put_iv
    else:
        atm_iv = None

    return call_iv, put_iv, atm_iv


def _safe_float(val: object) -> float | None:
    """Convert value to float, returning None on failure or NaN."""
    try:
        f = float(val)  # type: ignore[arg-type]
        return f if not math.isnan(f) else None
    except (TypeError, ValueError):
        return None


def _build_interpretation(
    current_iv: float | None,
    hv30: float | None,
    iv_rank: float | None,
    iv_percentile: float | None,
    iv_signal: IVSignal,
    put_call_skew: float | None,
) -> str:
    parts: list[str] = []

    if current_iv is not None:
        parts.append(f"当前 ATM IV {current_iv:.1f}%")
    elif hv30 is not None:
        parts.append(f"当前 HV30 {hv30:.1f}%（无期权数据，以 HV30 代理）")

    if iv_rank is not None:
        parts.append(f"IV Rank {iv_rank:.0f}/100")
    if iv_percentile is not None:
        parts.append(f"IV 百分位 {iv_percentile:.0f}%")

    if iv_signal == "buy_options":
        parts.append("波动率处于历史低位，期权定价偏低，适合做多波动率（买入期权）")
    elif iv_signal == "sell_options":
        parts.append("波动率处于历史高位，期权定价偏高，适合做空波动率（卖出期权策略）")
    elif iv_signal == "neutral":
        parts.append("波动率处于中性区间")

    if put_call_skew is not None:
        if put_call_skew > 2.0:
            parts.append(f"Put Skew +{put_call_skew:.1f}pp（市场偏向下行保护）")
        elif put_call_skew < -2.0:
            parts.append(f"Call Skew {put_call_skew:.1f}pp（市场偏向上行投机）")

    return "；".join(parts) + "。" if parts else "数据不可用。"


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------

def compute_iv_rank(ticker: str) -> IVRankData:
    """计算 IV Rank 及相关波动率指标。永不 raise，失败时 data_available=False。"""
    today_str = date.today().isoformat()
    _default = IVRankData(
        ticker=ticker.upper(),
        current_iv=None,
        iv_rank=None,
        iv_percentile=None,
        hv10=None,
        hv20=None,
        hv30=None,
        hv60=None,
        put_call_skew=None,
        term_structure=[],
        iv_signal="no_data",
        interpretation="数据不可用。",
        as_of_date=today_str,
        data_available=False,
    )

    try:
        tk = yf.Ticker(ticker)

        # ── 价格历史 ──────────────────────────────────────────────────────
        hist = tk.history(period="1y")
        if hist is None or hist.empty or len(hist) < 30:
            return _default

        prices = hist["Close"].dropna()

        # ── 历史波动率 ────────────────────────────────────────────────────
        hv10_s = _compute_hv(prices, 10)
        hv20_s = _compute_hv(prices, 20)
        hv30_s = _compute_hv(prices, 30)
        hv60_s = _compute_hv(prices, 60) if len(prices) >= 60 else pd.Series(dtype=float)

        hv10 = _safe_float(hv10_s.iloc[-1]) if not hv10_s.empty else None
        hv20 = _safe_float(hv20_s.iloc[-1]) if not hv20_s.empty else None
        hv30 = _safe_float(hv30_s.iloc[-1]) if not hv30_s.empty else None
        hv60 = _safe_float(hv60_s.iloc[-1]) if not hv60_s.empty else None

        # 转换为百分比
        hv10_pct = round(hv10 * 100, 2) if hv10 is not None else None
        hv20_pct = round(hv20 * 100, 2) if hv20 is not None else None
        hv30_pct = round(hv30 * 100, 2) if hv30 is not None else None
        hv60_pct = round(hv60 * 100, 2) if hv60 is not None else None

        # ── 当前价格 ──────────────────────────────────────────────────────
        try:
            info = tk.info
            spot = (
                _safe_float(info.get("regularMarketPrice"))
                or _safe_float(info.get("currentPrice"))
                or _safe_float(prices.iloc[-1])
            )
        except Exception:
            spot = _safe_float(prices.iloc[-1])

        if spot is None or spot <= 0:
            spot = float(prices.iloc[-1])

        # ── 期权链 & 期限结构 ─────────────────────────────────────────────
        current_iv_raw: float | None = None
        put_call_skew: float | None = None
        term_structure: list[TermStructurePoint] = []

        try:
            expiry_dates = tk.options  # list[str] | tuple[str, ...]
            if expiry_dates:
                today_date = date.today()
                processed = 0
                first_call_iv: float | None = None
                first_put_iv: float | None = None

                for expiry_str in expiry_dates:
                    if processed >= _MAX_EXPIRIES:
                        break
                    try:
                        expiry_date = date.fromisoformat(expiry_str)
                    except ValueError:
                        continue
                    dte = (expiry_date - today_date).days
                    if dte < _MIN_DTE:
                        continue

                    try:
                        chain = tk.option_chain(expiry_str)
                        call_iv, put_iv, atm_iv = _get_atm_iv(chain.calls, chain.puts, spot)
                    except Exception:
                        continue

                    if atm_iv is not None and atm_iv > 0:
                        term_structure.append(
                            TermStructurePoint(
                                expiry=expiry_str,
                                days_to_expiry=dte,
                                atm_iv=round(atm_iv * 100, 2),
                            )
                        )
                        if processed == 0:
                            current_iv_raw = atm_iv
                            first_call_iv = call_iv
                            first_put_iv = put_iv
                        processed += 1

                # Put/Call Skew from first valid expiry
                if first_call_iv is not None and first_put_iv is not None:
                    put_call_skew = round((first_put_iv - first_call_iv) * 100, 2)

        except Exception as e:
            logger.warning(f"[IVRank] Failed to get options for {ticker}: {e}")

        # ── IV Rank & Percentile ──────────────────────────────────────────
        # 使用 HV30 滚动序列作为历史 IV 代理（免费数据的最佳近似）
        hv30_series = _compute_hv(prices, 30).dropna()
        iv_rank: float | None = None
        iv_percentile: float | None = None

        # 用于排名的参考值：若有期权 IV 则用期权 IV，否则用当前 HV30
        reference_iv = current_iv_raw if current_iv_raw is not None else hv30
        current_iv_pct = round(current_iv_raw * 100, 2) if current_iv_raw is not None else None

        if reference_iv is not None and len(hv30_series) >= 30:
            min_hv = float(hv30_series.min())
            max_hv = float(hv30_series.max())

            if max_hv > min_hv:
                raw_rank = (reference_iv - min_hv) / (max_hv - min_hv) * 100.0
                iv_rank = round(max(0.0, min(100.0, raw_rank)), 1)

            below_pct = float((hv30_series < reference_iv).mean() * 100.0)
            iv_percentile = round(below_pct, 1)

        # ── 信号 ─────────────────────────────────────────────────────────
        if iv_rank is not None:
            if iv_rank < _BUY_THRESHOLD:
                signal: IVSignal = "buy_options"
            elif iv_rank > _SELL_THRESHOLD:
                signal = "sell_options"
            else:
                signal = "neutral"
        else:
            signal = "no_data"

        # ── 解读 ─────────────────────────────────────────────────────────
        interpretation = _build_interpretation(
            current_iv=current_iv_pct,
            hv30=hv30_pct,
            iv_rank=iv_rank,
            iv_percentile=iv_percentile,
            iv_signal=signal,
            put_call_skew=put_call_skew,
        )

        return IVRankData(
            ticker=ticker.upper(),
            current_iv=current_iv_pct,
            iv_rank=iv_rank,
            iv_percentile=iv_percentile,
            hv10=hv10_pct,
            hv20=hv20_pct,
            hv30=hv30_pct,
            hv60=hv60_pct,
            put_call_skew=put_call_skew,
            term_structure=term_structure,
            iv_signal=signal,
            interpretation=interpretation,
            as_of_date=today_str,
            data_available=True,
        )

    except Exception as e:
        logger.warning(f"[IVRank] Error computing IV rank for {ticker}: {e}")
        return _default

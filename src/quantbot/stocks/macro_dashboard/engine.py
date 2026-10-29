"""Macro Dashboard engine — Phase F.27.

实时抓取 VIX、收益率曲线、美元指数、黄金、原油，计算综合宏观情绪。
无需用户输入；自动聚合。

数据来源：yfinance 免费宏观符号（^VIX, ^TNX, ^IRX, DX-Y.NYB, GC=F, CL=F）
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
# 宏观符号
# ---------------------------------------------------------------------------

_SYMBOL_VIX = "^VIX"       # CBOE Volatility Index
_SYMBOL_TNX = "^TNX"       # 10-year Treasury yield (×10 = bps, divide by 10 = %)
_SYMBOL_IRX = "^IRX"       # 13-week Treasury Bill yield (proxy for 3-month)
_SYMBOL_DXY = "DX-Y.NYB"  # US Dollar Index
_SYMBOL_GOLD = "GC=F"      # Gold Futures
_SYMBOL_OIL = "CL=F"       # WTI Crude Oil Futures

# ---------------------------------------------------------------------------
# 类型
# ---------------------------------------------------------------------------

MacroRegime = Literal[
    "risk_on",
    "neutral",
    "risk_off",
    "extreme_risk_off",
    "unknown",
]

# 情绪阈值
_VIX_EXTREME = 30.0
_VIX_RISK_OFF = 20.0
_VIX_RISK_ON = 15.0
_VIX_PCT_HIGH = 70.0   # 70th percentile = elevated
_VIX_PCT_MID = 50.0
_VIX_PCT_LOW = 40.0


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class MacroDashboardData:
    vix: float | None                   # VIX 指数当前值
    vix_pct_52w: float | None           # VIX 在过去 52 周的百分位（0-100）
    yield_10y: float | None             # 10 年国债收益率（%）
    yield_3m: float | None              # 3 月国债收益率（%）
    yield_spread: float | None          # 10y - 3m 利差（百分比点）
    yield_curve_inverted: bool          # 利率曲线是否倒挂
    dxy: float | None                   # 美元指数
    gold: float | None                  # 黄金价格（美元/盎司）
    oil: float | None                   # WTI 原油价格（美元/桶）
    regime: MacroRegime                 # 综合宏观情绪
    interpretation: str
    as_of_date: str
    data_available: bool


# ---------------------------------------------------------------------------
# 内部计算
# ---------------------------------------------------------------------------

def _safe_last(hist: pd.DataFrame, col: str = "Close") -> float | None:
    """Get last non-NaN close from a history DataFrame."""
    if hist is None or hist.empty or col not in hist.columns:
        return None
    series = hist[col].dropna()
    if series.empty:
        return None
    v = float(series.iloc[-1])
    return v if not math.isnan(v) else None


def _vix_percentile(hist: pd.DataFrame) -> float | None:
    """Compute VIX current value's percentile within past 52 weeks of VIX history."""
    if hist is None or hist.empty or "Close" not in hist.columns:
        return None
    series = hist["Close"].dropna()
    if len(series) < 10:
        return None
    current = float(series.iloc[-1])
    pct = float((series < current).mean() * 100.0)
    return round(pct, 1)


def _classify_regime(
    vix: float | None,
    vix_pct: float | None,
    yield_spread: float | None,
    inverted: bool,
) -> MacroRegime:
    """Classify macro regime from VIX + yield curve."""
    if vix is None and yield_spread is None:
        return "unknown"

    # Extreme risk-off
    if vix is not None and vix >= _VIX_EXTREME:
        return "extreme_risk_off"
    if inverted and vix_pct is not None and vix_pct >= _VIX_PCT_HIGH:
        return "extreme_risk_off"

    # Risk-off
    if vix is not None and vix >= _VIX_RISK_OFF:
        return "risk_off"
    if vix_pct is not None and vix_pct >= _VIX_PCT_MID:
        return "risk_off"

    # Risk-on
    if (
        vix is not None and vix < _VIX_RISK_ON
        and (yield_spread is None or yield_spread > 0)
        and (vix_pct is None or vix_pct < _VIX_PCT_LOW)
    ):
        return "risk_on"

    return "neutral"


def _build_interpretation(
    vix: float | None,
    vix_pct: float | None,
    yield_spread: float | None,
    inverted: bool,
    dxy: float | None,
    gold: float | None,
    oil: float | None,
    regime: MacroRegime,
) -> str:
    parts: list[str] = []

    if vix is not None:
        pct_str = f"（{vix_pct:.0f}th 百分位）" if vix_pct is not None else ""
        parts.append(f"VIX {vix:.1f}{pct_str}")

    if yield_spread is not None:
        inv_str = "⚠ 倒挂" if inverted else "正常"
        parts.append(f"收益率曲线利差 {yield_spread:+.2f}pp（{inv_str}）")

    if dxy is not None:
        parts.append(f"DXY {dxy:.1f}")

    if gold is not None:
        parts.append(f"黄金 ${gold:,.0f}")

    if oil is not None:
        parts.append(f"WTI ${oil:.1f}")

    regime_str = {
        "risk_on": "→ 风险偏好（Risk-On），股票仓位可偏高",
        "neutral": "→ 中性宏观环境",
        "risk_off": "→ 风险规避（Risk-Off），建议降低仓位",
        "extreme_risk_off": "⚠ 极度避险（Extreme Risk-Off），大幅降仓或对冲",
        "unknown": "→ 宏观数据不足，无法判断",
    }
    parts.append(regime_str.get(regime, ""))

    return "；".join(p for p in parts if p) + "。" if parts else "数据不可用。"


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------

def compute_macro_dashboard() -> MacroDashboardData:
    """计算宏观仪表盘数据。永不 raise。部分数据失败时字段置 None，data_available=True。"""
    today_str = date.today().isoformat()

    _default = MacroDashboardData(
        vix=None,
        vix_pct_52w=None,
        yield_10y=None,
        yield_3m=None,
        yield_spread=None,
        yield_curve_inverted=False,
        dxy=None,
        gold=None,
        oil=None,
        regime="unknown",
        interpretation="数据不可用。",
        as_of_date=today_str,
        data_available=False,
    )

    any_data = False

    # ── VIX ─────────────────────────────────────────────────────────────────
    vix: float | None = None
    vix_pct: float | None = None
    try:
        vix_hist = yf.Ticker(_SYMBOL_VIX).history(period="1y")
        vix = _safe_last(vix_hist)
        vix_pct = _vix_percentile(vix_hist)
        if vix is not None:
            any_data = True
    except Exception as e:
        logger.warning(f"[Macro] VIX fetch failed: {e}")

    # ── 收益率曲线 ──────────────────────────────────────────────────────────
    yield_10y: float | None = None
    yield_3m: float | None = None
    try:
        tnx_hist = yf.Ticker(_SYMBOL_TNX).history(period="5d")
        raw = _safe_last(tnx_hist)
        # ^TNX quotes in percentage (e.g., 4.25 means 4.25%)
        yield_10y = round(raw, 4) if raw is not None else None
        if yield_10y is not None:
            any_data = True
    except Exception as e:
        logger.warning(f"[Macro] TNX fetch failed: {e}")

    try:
        irx_hist = yf.Ticker(_SYMBOL_IRX).history(period="5d")
        raw = _safe_last(irx_hist)
        yield_3m = round(raw, 4) if raw is not None else None
        if yield_3m is not None:
            any_data = True
    except Exception as e:
        logger.warning(f"[Macro] IRX fetch failed: {e}")

    yield_spread: float | None = None
    inverted = False
    if yield_10y is not None and yield_3m is not None:
        yield_spread = round(yield_10y - yield_3m, 4)
        inverted = yield_spread < 0.0

    # ── DXY ─────────────────────────────────────────────────────────────────
    dxy: float | None = None
    try:
        dxy_hist = yf.Ticker(_SYMBOL_DXY).history(period="5d")
        dxy = _safe_last(dxy_hist)
        if dxy is not None:
            dxy = round(dxy, 2)
            any_data = True
    except Exception as e:
        logger.warning(f"[Macro] DXY fetch failed: {e}")

    # ── Gold ─────────────────────────────────────────────────────────────────
    gold: float | None = None
    try:
        gold_hist = yf.Ticker(_SYMBOL_GOLD).history(period="5d")
        gold = _safe_last(gold_hist)
        if gold is not None:
            gold = round(gold, 2)
            any_data = True
    except Exception as e:
        logger.warning(f"[Macro] Gold fetch failed: {e}")

    # ── Oil ──────────────────────────────────────────────────────────────────
    oil: float | None = None
    try:
        oil_hist = yf.Ticker(_SYMBOL_OIL).history(period="5d")
        oil = _safe_last(oil_hist)
        if oil is not None:
            oil = round(oil, 2)
            any_data = True
    except Exception as e:
        logger.warning(f"[Macro] Oil fetch failed: {e}")

    if not any_data:
        return _default

    # ── 情绪分级 ─────────────────────────────────────────────────────────────
    regime = _classify_regime(vix, vix_pct, yield_spread, inverted)

    interpretation = _build_interpretation(
        vix=vix,
        vix_pct=vix_pct,
        yield_spread=yield_spread,
        inverted=inverted,
        dxy=dxy,
        gold=gold,
        oil=oil,
        regime=regime,
    )

    return MacroDashboardData(
        vix=round(vix, 2) if vix is not None else None,
        vix_pct_52w=vix_pct,
        yield_10y=yield_10y,
        yield_3m=yield_3m,
        yield_spread=yield_spread,
        yield_curve_inverted=inverted,
        dxy=dxy,
        gold=gold,
        oil=oil,
        regime=regime,
        interpretation=interpretation,
        as_of_date=today_str,
        data_available=True,
    )

"""Dividend Analysis engine — Phase F.28.

股息分析：股息率、Payout Ratio 安全性、5 年增长 CAGR、连续增长年数、Ex-Date 倒计时、
股息捕获信号。

数据来源：yfinance（tk.info + tk.dividends）
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Literal

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 类型
# ---------------------------------------------------------------------------

DividendSafety = Literal["safe", "watch", "danger", "no_dividend"]
DividendCaptureSignal = Literal["capture_opportunity", "not_applicable", "unknown"]

# 安全阈值
_SAFE_THRESHOLD = 0.75
_DANGER_THRESHOLD = 1.0

# 股息捕获：Ex-Date 前 N 天内视为机会
_CAPTURE_WINDOW_DAYS = 5

# 历史股息计算年数
_GROWTH_YEARS = 5


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class DividendAnalysisData:
    ticker: str
    dividend_yield: float | None          # 年化股息率（0-1 小数）
    annual_dividend: float | None         # 年化股息金额（美元/股）
    payout_ratio: float | None            # 派息率（0-1 小数）
    ex_dividend_date: str | None          # Ex-Date ISO 字符串
    days_to_ex_date: int | None           # 距 Ex-Date 天数（负 = 已过）
    dividend_frequency: int | None        # 年派息次数（估算）
    five_yr_growth_rate: float | None     # 5 年股息 CAGR（小数，如 0.08 = 8%）
    consecutive_growth_years: int         # 连续增长年数
    safety: DividendSafety                # 派息安全性评级
    capture_signal: DividendCaptureSignal # 股息捕获信号
    interpretation: str
    as_of_date: str
    data_available: bool
    historical_annual: list[dict] = field(default_factory=list)
    # [{"year": 2023, "total": 4.32}, ...] 近 5 年年度股息


# ---------------------------------------------------------------------------
# 内部计算
# ---------------------------------------------------------------------------

def _annual_dividends(dividends: pd.Series) -> dict[int, float]:
    """将股息 Series 按年聚合，返回 {year: total_dividend}。"""
    if dividends is None or dividends.empty:
        return {}
    # 确保 index 是 DatetimeIndex
    if not isinstance(dividends.index, pd.DatetimeIndex):
        try:
            dividends.index = pd.to_datetime(dividends.index, utc=True)
        except Exception:
            return {}
    annual: dict[int, float] = {}
    for ts, val in dividends.items():
        try:
            yr = int(ts.year)  # type: ignore[union-attr]
            annual[yr] = annual.get(yr, 0.0) + float(val)
        except Exception:
            continue
    return annual


def _compute_dividend_growth(annual: dict[int, float]) -> float | None:
    """计算 5 年 CAGR：最早年 vs 最近年的年度股息增长率。

    需要至少 2 个年度数据。
    若不足 5 年则用实际可用年数。
    """
    if len(annual) < 2:
        return None

    years_sorted = sorted(annual.keys())
    # 取最近 _GROWTH_YEARS+1 年的数据
    if len(years_sorted) > _GROWTH_YEARS + 1:
        years_sorted = years_sorted[-(  _GROWTH_YEARS + 1):]

    start_year = years_sorted[0]
    end_year = years_sorted[-1]
    n_years = end_year - start_year

    if n_years <= 0:
        return None

    start_div = annual[start_year]
    end_div = annual[end_year]

    if start_div <= 0 or end_div <= 0:
        return None

    try:
        cagr = (end_div / start_div) ** (1.0 / n_years) - 1.0
        return round(cagr, 6)
    except Exception:
        return None


def _compute_consecutive_growth(annual: dict[int, float]) -> int:
    """计算连续增长年数（逐年对比年度总股息）。"""
    if len(annual) < 2:
        return 0

    years_sorted = sorted(annual.keys(), reverse=True)
    count = 0
    for i in range(len(years_sorted) - 1):
        cur_year = years_sorted[i]
        prev_year = years_sorted[i + 1]
        # 只计算相邻年份
        if cur_year - prev_year != 1:
            break
        if annual[cur_year] > annual[prev_year]:
            count += 1
        else:
            break
    return count


def _classify_safety(
    payout_ratio: float | None,
    dividend_yield: float | None,
) -> DividendSafety:
    """分类派息安全性。"""
    if dividend_yield is None or dividend_yield <= 0:
        return "no_dividend"
    if payout_ratio is None:
        return "no_dividend"
    if payout_ratio < _SAFE_THRESHOLD:
        return "safe"
    if payout_ratio < _DANGER_THRESHOLD:
        return "watch"
    return "danger"


def _classify_capture(days_to_ex: int | None) -> DividendCaptureSignal:
    """判断股息捕获信号。"""
    if days_to_ex is None:
        return "unknown"
    if 0 <= days_to_ex <= _CAPTURE_WINDOW_DAYS:
        return "capture_opportunity"
    return "not_applicable"


def _parse_ex_date(ex_timestamp: object) -> date | None:
    """解析 yfinance 返回的 exDividendDate（Unix 时间戳 int 或其他格式）。"""
    if ex_timestamp is None:
        return None
    try:
        if isinstance(ex_timestamp, (int, float)):
            if math.isnan(float(ex_timestamp)):
                return None
            return datetime.fromtimestamp(int(ex_timestamp), tz=timezone.utc).date()
        if isinstance(ex_timestamp, datetime):
            return ex_timestamp.date()
        if isinstance(ex_timestamp, date):
            return ex_timestamp
        if isinstance(ex_timestamp, str):
            return date.fromisoformat(ex_timestamp[:10])
    except Exception:
        pass
    return None


def _estimate_frequency(dividends: pd.Series) -> int | None:
    """估算年派息次数（基于历史记录）。"""
    if dividends is None or dividends.empty:
        return None
    if not isinstance(dividends.index, pd.DatetimeIndex):
        try:
            dividends.index = pd.to_datetime(dividends.index, utc=True)
        except Exception:
            return None
    # 用最近 2 年的记录计数 / 2 作为估算
    two_yr_ago = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=730)
    recent = dividends[dividends.index >= two_yr_ago]
    if len(recent) == 0:
        return None
    freq = round(len(recent) / 2)
    return max(1, freq)


def _build_interpretation(
    ticker: str,
    dividend_yield: float | None,
    payout_ratio: float | None,
    safety: DividendSafety,
    capture_signal: DividendCaptureSignal,
    days_to_ex: int | None,
    five_yr_growth: float | None,
    consecutive_years: int,
    ex_date_str: str | None,
) -> str:
    parts: list[str] = []

    if safety == "no_dividend":
        parts.append(f"{ticker} 当前不派息")
    else:
        if dividend_yield is not None:
            parts.append(f"股息率 {dividend_yield * 100:.2f}%")
        if payout_ratio is not None:
            safety_label = {"safe": "安全", "watch": "观察", "danger": "危险"}.get(safety, "")
            parts.append(f"派息率 {payout_ratio * 100:.1f}%（{safety_label}）")

        if five_yr_growth is not None:
            parts.append(f"5 年股息 CAGR {five_yr_growth * 100:.1f}%")
        if consecutive_years >= 25:
            parts.append(f"连续增长 {consecutive_years} 年（Dividend Aristocrat）")
        elif consecutive_years >= 10:
            parts.append(f"连续增长 {consecutive_years} 年")
        elif consecutive_years > 0:
            parts.append(f"连续增长 {consecutive_years} 年")

        if capture_signal == "capture_opportunity" and days_to_ex is not None:
            parts.append(f"⚡ 股息捕获机会：Ex-Date {ex_date_str}（{days_to_ex} 天后）")
        elif ex_date_str:
            if days_to_ex is not None and days_to_ex < 0:
                parts.append(f"Ex-Date 已过（{ex_date_str}）")
            else:
                parts.append(f"Ex-Date {ex_date_str}")

    return "；".join(parts) + "。" if parts else "数据不足。"


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------

def compute_dividend_analysis(ticker: str) -> DividendAnalysisData:
    """计算股息分析数据。永不 raise。yfinance 失败时 data_available=False。"""
    today = date.today()
    today_str = today.isoformat()

    _default = DividendAnalysisData(
        ticker=ticker,
        dividend_yield=None,
        annual_dividend=None,
        payout_ratio=None,
        ex_dividend_date=None,
        days_to_ex_date=None,
        dividend_frequency=None,
        five_yr_growth_rate=None,
        consecutive_growth_years=0,
        safety="no_dividend",
        capture_signal="unknown",
        interpretation="数据不可用。",
        as_of_date=today_str,
        data_available=False,
    )

    try:
        tk = yf.Ticker(ticker)
        info: dict = tk.info or {}
        dividends: pd.Series = tk.dividends
    except Exception as e:
        logger.warning(f"[Dividend] yfinance fetch failed for {ticker}: {e}")
        return _default

    # ── 基本指标 ─────────────────────────────────────────────────────────────
    dividend_yield: float | None = None
    raw_yield = info.get("dividendYield")
    if raw_yield is not None:
        try:
            v = float(raw_yield)
            if not math.isnan(v) and v > 0:
                dividend_yield = round(v, 6)
        except Exception:
            pass

    annual_dividend: float | None = None
    raw_rate = info.get("dividendRate")
    if raw_rate is not None:
        try:
            v = float(raw_rate)
            if not math.isnan(v) and v > 0:
                annual_dividend = round(v, 4)
        except Exception:
            pass

    payout_ratio: float | None = None
    raw_pr = info.get("payoutRatio")
    if raw_pr is not None:
        try:
            v = float(raw_pr)
            if not math.isnan(v) and v >= 0:
                payout_ratio = round(v, 6)
        except Exception:
            pass

    # ── Ex-Date ───────────────────────────────────────────────────────────────
    ex_date: date | None = _parse_ex_date(info.get("exDividendDate"))
    ex_dividend_date_str: str | None = ex_date.isoformat() if ex_date else None
    days_to_ex: int | None = (ex_date - today).days if ex_date else None

    # ── 历史股息 ─────────────────────────────────────────────────────────────
    annual = _annual_dividends(dividends)
    five_yr_growth = _compute_dividend_growth(annual)
    consecutive_years = _compute_consecutive_growth(annual)
    frequency = _estimate_frequency(dividends)

    # 近 5 年年度股息（用于前端展示）
    recent_years = sorted(annual.keys())[-_GROWTH_YEARS:]
    historical_annual = [{"year": yr, "total": round(annual[yr], 4)} for yr in recent_years]

    # ── 分类 ─────────────────────────────────────────────────────────────────
    safety = _classify_safety(payout_ratio, dividend_yield)
    capture_signal = _classify_capture(days_to_ex)

    interpretation = _build_interpretation(
        ticker=ticker,
        dividend_yield=dividend_yield,
        payout_ratio=payout_ratio,
        safety=safety,
        capture_signal=capture_signal,
        days_to_ex=days_to_ex,
        five_yr_growth=five_yr_growth,
        consecutive_years=consecutive_years,
        ex_date_str=ex_dividend_date_str,
    )

    return DividendAnalysisData(
        ticker=ticker,
        dividend_yield=dividend_yield,
        annual_dividend=annual_dividend,
        payout_ratio=payout_ratio,
        ex_dividend_date=ex_dividend_date_str,
        days_to_ex_date=days_to_ex,
        dividend_frequency=frequency,
        five_yr_growth_rate=five_yr_growth,
        consecutive_growth_years=consecutive_years,
        safety=safety,
        capture_signal=capture_signal,
        interpretation=interpretation,
        as_of_date=today_str,
        data_available=True,
        historical_annual=historical_annual,
    )

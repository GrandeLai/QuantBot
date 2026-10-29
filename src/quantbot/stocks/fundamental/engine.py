"""基本面 Alpha 引擎（Phase F.4）.

功能：
1. PEAD（Post-Earnings Announcement Drift）：
   - 获取历史 EPS surprise，评估 surprise 幅度
   - 基于历史同类 surprise 后的价格漂移，估算未来漂移方向和强度

2. Piotroski F-Score：
   - 9 维财务健康指标（盈利能力 4 + 杠杆/流动性 3 + 运营效率 2）
   - 评分 0-9：≥7 优质，4-6 中等，≤3 红旗

数据来源：yfinance（免费，无 API key 要求）

免责声明：本模块输出仅供参考，不构成投资建议。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

import pandas as pd
import yfinance as yf
from loguru import logger

# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class EarningsSurprise:
    """单季度 EPS surprise 记录."""

    ticker: str
    quarter: date          # 报告季度结束日期
    eps_actual: float
    eps_estimate: float
    eps_difference: float
    surprise_pct: float    # (actual - estimate) / abs(estimate)，正 = 超预期


@dataclass
class PEADSignal:
    """PEAD 信号 — 基于最新 EPS surprise 推断后续漂移方向."""

    ticker: str
    latest_surprise: EarningsSurprise
    surprise_magnitude: Literal["large_beat", "beat", "inline", "miss", "large_miss"]
    historical_drift_7d: float | None    # 历史同类 surprise 后 7 日平均超额（%）
    historical_drift_30d: float | None
    historical_drift_60d: float | None
    signal_strength: float               # 0-1，基于 surprise 幅度和历史置信度


@dataclass
class PiotroskiScore:
    """Piotroski F-Score — 9 维财务健康评分."""

    ticker: str
    score: int                          # 0-9
    grade: Literal["strong", "moderate", "weak"]
    signals: dict[str, bool]            # 9 子信号明细
    as_of_date: date
    interpretation: str


# ---------------------------------------------------------------------------
# 阈值常量
# ---------------------------------------------------------------------------

_LARGE_BEAT_THRESHOLD = 0.05   # surprise_pct ≥ 5% → large_beat
_BEAT_THRESHOLD = 0.01         # ≥1% → beat
_MISS_THRESHOLD = -0.01        # ≤-1% → miss
_LARGE_MISS_THRESHOLD = -0.05  # ≤-5% → large_miss

_PIOTROSKI_STRONG = 7
_PIOTROSKI_WEAK = 3


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _safe_float(value: object, default: float = 0.0) -> float:
    """安全转换为 float，失败返回 default."""
    try:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return default
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _classify_surprise(surprise_pct: float) -> Literal["large_beat", "beat", "inline", "miss", "large_miss"]:
    if surprise_pct >= _LARGE_BEAT_THRESHOLD:
        return "large_beat"
    if surprise_pct >= _BEAT_THRESHOLD:
        return "beat"
    if surprise_pct <= _LARGE_MISS_THRESHOLD:
        return "large_miss"
    if surprise_pct <= _MISS_THRESHOLD:
        return "miss"
    return "inline"


def _signal_strength_from_surprise(surprise_pct: float) -> float:
    """将 surprise_pct 映射到 [0, 1] 信号强度.

    ≥10% → 1.0；≤-10% → 1.0（方向由 magnitude 决定）；0% → 0.1
    """
    abs_pct = abs(surprise_pct)
    # 线性映射 0%→0.1，10%→1.0
    strength = 0.1 + min(abs_pct / 0.10, 0.9)
    return round(min(strength, 1.0), 3)


def _compute_historical_drift(
    ticker: yf.Ticker,
    surprise_magnitude: Literal["large_beat", "beat", "inline", "miss", "large_miss"],
    earnings_df: pd.DataFrame,
    *,
    windows: tuple[int, ...] = (7, 30, 60),
) -> dict[int, float | None]:
    """计算历史同类 surprise 后的平均漂移（%）.

    方法：
    1. 对每个历史季度 surprise，归类 magnitude
    2. 取同类 magnitude 的所有公告日，获取公告后 N 日收益率
    3. 平均作为历史漂移估算
    """
    result: dict[int, float | None] = {w: None for w in windows}

    try:
        # 找出同类 surprise 的历史季度（排除最近一个，那是 latest）
        matching_quarters: list[date] = []
        for q_date, row in earnings_df.iterrows():
            q = pd.Timestamp(q_date).date() if not isinstance(q_date, date) else q_date
            sp = _safe_float(row.get("surprisePercent", 0.0))
            mag = _classify_surprise(sp)
            if mag == surprise_magnitude:
                matching_quarters.append(q)

        if len(matching_quarters) < 2:
            return result

        # 获取价格历史
        history = ticker.history(period="5y", interval="1d")
        if history.empty:
            return result
        history.index = pd.to_datetime(history.index).tz_localize(None)

        for window in windows:
            drifts: list[float] = []
            for q in matching_quarters[:-1]:  # 排除最新季度
                q_ts = pd.Timestamp(q)
                # 找最近的交易日
                idx = history.index.searchsorted(q_ts)
                if idx >= len(history) - window - 1:
                    continue
                entry_price = history["Close"].iloc[idx]
                exit_price = history["Close"].iloc[idx + window]
                if entry_price > 0:
                    drift_pct = (exit_price - entry_price) / entry_price * 100
                    drifts.append(drift_pct)

            if drifts:
                result[window] = round(sum(drifts) / len(drifts), 2)

    except Exception as exc:  # noqa: BLE001
        logger.debug(f"历史漂移计算失败: {exc}")

    return result


# ---------------------------------------------------------------------------
# 核心函数：PEAD
# ---------------------------------------------------------------------------


def compute_pead_signal(
    ticker: str,
    *,
    lookback_quarters: int = 8,
    surprise_large_threshold: float = _LARGE_BEAT_THRESHOLD,
) -> PEADSignal | None:
    """获取最新 EPS surprise 并估算历史 PEAD 漂移.

    Args:
        ticker: 证券代码
        lookback_quarters: 用于计算历史漂移的历史季度数
        surprise_large_threshold: large_beat/miss 阈值（默认 5%）

    Returns:
        PEADSignal，若无 EPS 数据则返回 None
    """
    try:
        t = yf.Ticker(ticker.upper())
        eh = t.get_earnings_history()

        if eh is None or eh.empty:
            return None

        # 取最新一个季度
        latest_row = eh.iloc[-1]
        quarter_index = eh.index[-1]
        q_date: date
        if isinstance(quarter_index, (pd.Timestamp, datetime)):
            q_date = pd.Timestamp(quarter_index).date()
        else:
            q_date = date.fromisoformat(str(quarter_index))

        eps_actual = _safe_float(latest_row.get("epsActual", 0.0))
        eps_estimate = _safe_float(latest_row.get("epsEstimate", 0.0))
        eps_diff = eps_actual - eps_estimate
        sp = _safe_float(latest_row.get("surprisePercent", eps_diff / max(abs(eps_estimate), 1e-9)))

        surprise = EarningsSurprise(
            ticker=ticker.upper(),
            quarter=q_date,
            eps_actual=eps_actual,
            eps_estimate=eps_estimate,
            eps_difference=eps_diff,
            surprise_pct=sp,
        )

        magnitude = _classify_surprise(sp)
        strength = _signal_strength_from_surprise(sp)

        # 计算历史漂移
        drifts = _compute_historical_drift(t, magnitude, eh)

        return PEADSignal(
            ticker=ticker.upper(),
            latest_surprise=surprise,
            surprise_magnitude=magnitude,
            historical_drift_7d=drifts.get(7),
            historical_drift_30d=drifts.get(30),
            historical_drift_60d=drifts.get(60),
            signal_strength=strength,
        )

    except Exception as exc:  # noqa: BLE001
        logger.warning(f"compute_pead_signal({ticker}) 失败: {exc}")
        return None


# ---------------------------------------------------------------------------
# 核心函数：Piotroski F-Score
# ---------------------------------------------------------------------------


def compute_piotroski_fscore(ticker: str) -> PiotroskiScore | None:
    """计算 Piotroski F-Score（9 维财务健康评分）.

    Args:
        ticker: 证券代码

    Returns:
        PiotroskiScore，若财务数据不足则返回 None
    """
    try:
        t = yf.Ticker(ticker.upper())
        fin = t.get_financials(freq="annual")
        bs = t.get_balance_sheet(freq="annual")
        cf = t.get_cash_flow(freq="annual")

        if fin is None or fin.empty or bs is None or bs.empty or cf is None or cf.empty:
            return None

        # 需要至少 2 年数据做 Δ 计算
        def _row(df: pd.DataFrame, *keys: str) -> pd.Series:
            for k in keys:
                if k in df.index:
                    return df.loc[k]
            return pd.Series(dtype=float)

        def _val(df: pd.DataFrame, *keys: str, year: int = 0) -> float:
            s = _row(df, *keys)
            if s.empty or len(s) <= year:
                return float("nan")
            return _safe_float(s.iloc[year], float("nan"))

        # 最新年 (year=0) vs 上一年 (year=1)
        net_income_0 = _val(fin, "NetIncome", "NetIncomeFromContinuingOperationNetMinorityInterest")
        net_income_1 = _val(fin, "NetIncome", "NetIncomeFromContinuingOperationNetMinorityInterest", year=1)

        total_assets_0 = _val(bs, "TotalAssets")
        total_assets_1 = _val(bs, "TotalAssets", year=1)
        total_assets_avg = (total_assets_0 + total_assets_1) / 2 if not (math.isnan(total_assets_0) or math.isnan(total_assets_1)) else total_assets_0

        operating_cf_0 = _val(cf, "OperatingCashFlow", "CashFlowFromContinuingOperatingActivities")

        long_term_debt_0 = _val(bs, "LongTermDebt", "LongTermDebtAndCapitalLeaseObligation")
        long_term_debt_1 = _val(bs, "LongTermDebt", "LongTermDebtAndCapitalLeaseObligation", year=1)

        current_assets_0 = _val(bs, "CurrentAssets")
        current_assets_1 = _val(bs, "CurrentAssets", year=1)
        current_liab_0 = _val(bs, "CurrentLiabilities")
        current_liab_1 = _val(bs, "CurrentLiabilities", year=1)

        shares_0 = _val(bs, "OrdinarySharesNumber", "ShareIssued")
        shares_1 = _val(bs, "OrdinarySharesNumber", "ShareIssued", year=1)

        revenue_0 = _val(fin, "TotalRevenue")
        revenue_1 = _val(fin, "TotalRevenue", year=1)
        gross_profit_0 = _val(fin, "GrossProfit")
        gross_profit_1 = _val(fin, "GrossProfit", year=1)

        # --- 计算各指标 ---
        roa_0 = net_income_0 / total_assets_avg if total_assets_avg and not math.isnan(total_assets_avg) else float("nan")
        # 上年 ROA（用上上年总资产近似）
        roa_1 = net_income_1 / total_assets_1 if total_assets_1 and not math.isnan(total_assets_1) else float("nan")

        leverage_0 = long_term_debt_0 / total_assets_0 if total_assets_0 and not math.isnan(total_assets_0) else float("nan")
        leverage_1 = long_term_debt_1 / total_assets_1 if total_assets_1 and not math.isnan(total_assets_1) else float("nan")

        current_ratio_0 = current_assets_0 / current_liab_0 if current_liab_0 and not math.isnan(current_liab_0) else float("nan")
        current_ratio_1 = current_assets_1 / current_liab_1 if current_liab_1 and not math.isnan(current_liab_1) else float("nan")

        gross_margin_0 = gross_profit_0 / revenue_0 if revenue_0 and not math.isnan(revenue_0) else float("nan")
        gross_margin_1 = gross_profit_1 / revenue_1 if revenue_1 and not math.isnan(revenue_1) else float("nan")

        asset_turnover_0 = revenue_0 / total_assets_0 if total_assets_0 and not math.isnan(total_assets_0) else float("nan")
        asset_turnover_1 = revenue_1 / total_assets_1 if total_assets_1 and not math.isnan(total_assets_1) else float("nan")

        # --- 9 个二进制信号 ---
        def _b(cond: object) -> bool:
            """安全 bool，含 NaN 的条件返回 False."""
            try:
                return bool(cond) and not math.isnan(float(cond))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return False

        signals: dict[str, bool] = {
            # 盈利能力
            "F1_roa_positive": _b(roa_0 > 0),
            "F2_operating_cashflow_positive": _b(operating_cf_0 > 0),
            "F3_roa_improving": _b(not math.isnan(roa_0) and not math.isnan(roa_1) and roa_0 > roa_1),
            "F4_accruals_low": _b(not math.isnan(operating_cf_0) and not math.isnan(net_income_0)
                                  and operating_cf_0 > net_income_0),
            # 杠杆 / 流动性
            "F5_leverage_decreasing": _b(not math.isnan(leverage_0) and not math.isnan(leverage_1)
                                         and leverage_0 < leverage_1),
            "F6_current_ratio_improving": _b(not math.isnan(current_ratio_0) and not math.isnan(current_ratio_1)
                                              and current_ratio_0 > current_ratio_1),
            "F7_no_dilution": _b(not math.isnan(shares_0) and not math.isnan(shares_1)
                                 and shares_0 <= shares_1),
            # 运营效率
            "F8_gross_margin_improving": _b(not math.isnan(gross_margin_0) and not math.isnan(gross_margin_1)
                                            and gross_margin_0 > gross_margin_1),
            "F9_asset_turnover_improving": _b(not math.isnan(asset_turnover_0) and not math.isnan(asset_turnover_1)
                                              and asset_turnover_0 > asset_turnover_1),
        }

        score = sum(1 for v in signals.values() if v)

        if score >= _PIOTROSKI_STRONG:
            grade: Literal["strong", "moderate", "weak"] = "strong"
            interpretation = f"财务健康优质（F-Score {score}/9），历史上此类股票有超额收益潜力。"
        elif score <= _PIOTROSKI_WEAK:
            grade = "weak"
            interpretation = f"财务红旗（F-Score {score}/9），建议谨慎，历史上此类股票表现偏弱。"
        else:
            grade = "moderate"
            interpretation = f"财务状况中等（F-Score {score}/9），无明显超额信号。"

        return PiotroskiScore(
            ticker=ticker.upper(),
            score=score,
            grade=grade,
            signals=signals,
            as_of_date=date.today(),
            interpretation=interpretation,
        )

    except Exception as exc:  # noqa: BLE001
        logger.warning(f"compute_piotroski_fscore({ticker}) 失败: {exc}")
        return None

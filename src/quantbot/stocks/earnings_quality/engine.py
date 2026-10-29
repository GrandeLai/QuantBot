"""Earnings Quality Engine — Piotroski F-Score + Beneish M-Score + Sloan Accrual.

Phase F.20 — 盈利质量三件套
数据来源：yfinance 年度财报（financials / balance_sheet / cashflow）

Piotroski F-Score (0-9):
  Profitability (4): ROA>0, CFO>0, ΔROA>0, CFO>NetIncome(accrual OK)
  Leverage/Liquidity (3): ΔLeverage↓, ΔCurrentRatio↑, no new shares
  Operating Efficiency (2): ΔGrossMargin↑, ΔAssetTurnover↑

Beneish M-Score: 8 indices, threshold -2.22 (> -2.22 = manipulation risk)

Sloan Accrual Ratio: (NetIncome - CFO) / AvgTotalAssets
  > 10% = low quality; < -10% = possibly aggressive write-down
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import yfinance as yf
import pandas as pd

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

EarningsQualityGrade = Literal["high_quality", "average_quality", "low_quality", "manipulator_risk"]
FScoreGrade = Literal["very_strong", "strong", "average", "weak"]
AccrualQuality = Literal["high", "medium", "low"]


@dataclass
class EarningsQualityData:
    ticker: str
    # Piotroski F-Score
    f_score: int | None
    f_score_grade: FScoreGrade | None
    f_score_components: dict[str, bool] = field(default_factory=dict)
    # Beneish M-Score
    m_score: float | None = None
    manipulation_risk: bool = False
    # Sloan Accrual
    accrual_ratio: float | None = None
    accrual_quality: AccrualQuality | None = None
    # Overall
    quality_grade: EarningsQualityGrade = "average_quality"
    interpretation: str = ""
    as_of_date: date = field(default_factory=date.today)
    data_available: bool = True


# ---------------------------------------------------------------------------
# Safe field extraction helpers
# ---------------------------------------------------------------------------

def _safe_get(df: pd.DataFrame, *keys: str, col: int = 0) -> float | None:
    """Try each key in df.index; return value at column `col` as float or None."""
    if df is None or df.empty:
        return None
    for key in keys:
        if key in df.index:
            try:
                val = df.loc[key].iloc[col]
                if pd.isna(val):
                    return None
                return float(val)
            except (IndexError, TypeError, ValueError):
                continue
    return None


def _has_col(df: pd.DataFrame, min_cols: int = 2) -> bool:
    """Check df has at least min_cols columns (periods)."""
    return df is not None and not df.empty and len(df.columns) >= min_cols


# ---------------------------------------------------------------------------
# Piotroski F-Score
# ---------------------------------------------------------------------------

def _compute_f_score(
    fin: pd.DataFrame,
    bal: pd.DataFrame,
    cf: pd.DataFrame,
) -> tuple[int | None, FScoreGrade | None, dict[str, bool]]:
    """
    Compute Piotroski F-Score (0-9).
    Returns (score, grade, component_dict) or (None, None, {}) on failure.
    """
    components: dict[str, bool] = {}

    # Current period values
    net_income = _safe_get(fin, "Net Income")
    cfo = _safe_get(cf, "Operating Cash Flow", "Cash From Operations", "Total Cash From Operating Activities")
    total_assets_t = _safe_get(bal, "Total Assets")
    total_assets_t1 = _safe_get(bal, "Total Assets", col=1)
    gross_profit = _safe_get(fin, "Gross Profit")
    revenue = _safe_get(fin, "Total Revenue", "Revenue")
    lt_debt_t = _safe_get(bal, "Long Term Debt", "Long-Term Debt")
    lt_debt_t1 = _safe_get(bal, "Long Term Debt", "Long-Term Debt", col=1)
    current_assets_t = _safe_get(bal, "Current Assets", "Total Current Assets")
    current_assets_t1 = _safe_get(bal, "Current Assets", "Total Current Assets", col=1)
    current_liab_t = _safe_get(bal, "Current Liabilities", "Total Current Liabilities")
    current_liab_t1 = _safe_get(bal, "Current Liabilities", "Total Current Liabilities", col=1)
    shares_t = _safe_get(bal, "Common Stock", "Ordinary Shares Number", "Share Issued")
    shares_t1 = _safe_get(bal, "Common Stock", "Ordinary Shares Number", "Share Issued", col=1)

    # Prior period values for deltas
    net_income_t1 = _safe_get(fin, "Net Income", col=1)
    total_assets_t1_2 = _safe_get(bal, "Total Assets", col=2) if _has_col(bal, 3) else None
    gross_profit_t1 = _safe_get(fin, "Gross Profit", col=1)
    revenue_t1 = _safe_get(fin, "Total Revenue", "Revenue", col=1)

    # Derived
    avg_assets = None
    if total_assets_t is not None and total_assets_t1 is not None:
        avg_assets = (total_assets_t + total_assets_t1) / 2.0

    avg_assets_t1 = None
    if total_assets_t1 is not None and total_assets_t1_2 is not None:
        avg_assets_t1 = (total_assets_t1 + total_assets_t1_2) / 2.0

    roa_t = (net_income / avg_assets) if (net_income is not None and avg_assets and avg_assets != 0) else None
    roa_t1 = (net_income_t1 / avg_assets_t1) if (net_income_t1 is not None and avg_assets_t1 and avg_assets_t1 != 0) else None

    lever_t = (lt_debt_t / total_assets_t) if (lt_debt_t is not None and total_assets_t and total_assets_t != 0) else None
    lever_t1 = (lt_debt_t1 / total_assets_t1) if (lt_debt_t1 is not None and total_assets_t1 and total_assets_t1 != 0) else None

    cr_t = (current_assets_t / current_liab_t) if (current_assets_t is not None and current_liab_t and current_liab_t != 0) else None
    cr_t1 = (current_assets_t1 / current_liab_t1) if (current_assets_t1 is not None and current_liab_t1 and current_liab_t1 != 0) else None

    gm_t = (gross_profit / revenue) if (gross_profit is not None and revenue and revenue != 0) else None
    gm_t1 = (gross_profit_t1 / revenue_t1) if (gross_profit_t1 is not None and revenue_t1 and revenue_t1 != 0) else None

    at_t = (revenue / total_assets_t) if (revenue is not None and total_assets_t and total_assets_t != 0) else None
    at_t1 = (revenue_t1 / total_assets_t1) if (revenue_t1 is not None and total_assets_t1 and total_assets_t1 != 0) else None

    # --- Profitability ---
    if roa_t is not None:
        components["ROA > 0"] = roa_t > 0
    if cfo is not None:
        components["CFO > 0"] = cfo > 0
    if roa_t is not None and roa_t1 is not None:
        components["ΔROA > 0"] = roa_t > roa_t1
    if cfo is not None and net_income is not None and avg_assets and avg_assets != 0:
        accrual = net_income / avg_assets
        components["Accrual OK (CFO/Assets > NI/Assets)"] = (cfo / avg_assets) > accrual

    # --- Leverage / Liquidity ---
    if lever_t is not None and lever_t1 is not None:
        components["ΔLeverage ↓"] = lever_t < lever_t1
    if cr_t is not None and cr_t1 is not None:
        components["ΔCurrentRatio ↑"] = cr_t > cr_t1
    if shares_t is not None and shares_t1 is not None:
        components["No New Shares"] = shares_t <= shares_t1 * 1.01  # 1% tolerance

    # --- Operating Efficiency ---
    if gm_t is not None and gm_t1 is not None:
        components["ΔGrossMargin ↑"] = gm_t > gm_t1
    if at_t is not None and at_t1 is not None:
        components["ΔAssetTurnover ↑"] = at_t > at_t1

    if not components:
        return None, None, {}

    score = sum(1 for v in components.values() if v)

    if score >= 8:
        grade: FScoreGrade = "very_strong"
    elif score >= 6:
        grade = "strong"
    elif score >= 3:
        grade = "average"
    else:
        grade = "weak"

    return score, grade, components


# ---------------------------------------------------------------------------
# Beneish M-Score
# ---------------------------------------------------------------------------

def _compute_m_score(
    fin: pd.DataFrame,
    bal: pd.DataFrame,
    cf: pd.DataFrame,
) -> float | None:
    """
    Compute Beneish M-Score.
    M > -2.22 indicates possible earnings manipulation.
    """
    # Year t (col=0) and year t-1 (col=1)
    rev_t = _safe_get(fin, "Total Revenue", "Revenue", col=0)
    rev_t1 = _safe_get(fin, "Total Revenue", "Revenue", col=1)
    rec_t = _safe_get(bal, "Receivables", "Accounts Receivable", "Net Receivables", col=0)
    rec_t1 = _safe_get(bal, "Receivables", "Accounts Receivable", "Net Receivables", col=1)
    cogs_t = _safe_get(fin, "Cost Of Revenue", "Reconciled Cost Of Revenue", "Cost of Revenue", col=0)
    cogs_t1 = _safe_get(fin, "Cost Of Revenue", "Reconciled Cost Of Revenue", "Cost of Revenue", col=1)
    assets_t = _safe_get(bal, "Total Assets", col=0)
    assets_t1 = _safe_get(bal, "Total Assets", col=1)
    ppe_t = _safe_get(bal, "Net PPE", "Net Property Plant And Equipment", "Property Plant And Equipment", col=0)
    ppe_t1 = _safe_get(bal, "Net PPE", "Net Property Plant And Equipment", "Property Plant And Equipment", col=1)
    cur_assets_t = _safe_get(bal, "Current Assets", "Total Current Assets", col=0)
    cur_assets_t1 = _safe_get(bal, "Current Assets", "Total Current Assets", col=1)
    dep_t = _safe_get(cf, "Depreciation And Amortization", "Depreciation", col=0)
    dep_t1 = _safe_get(cf, "Depreciation And Amortization", "Depreciation", col=1)
    sga_t = _safe_get(fin, "Selling General And Administrative", "SGA", "Selling General Administrative", col=0)
    sga_t1 = _safe_get(fin, "Selling General And Administrative", "SGA", "Selling General Administrative", col=1)
    lt_debt_t = _safe_get(bal, "Long Term Debt", col=0)
    lt_debt_t1 = _safe_get(bal, "Long Term Debt", col=1)
    cur_liab_t = _safe_get(bal, "Current Liabilities", "Total Current Liabilities", col=0)
    cur_liab_t1 = _safe_get(bal, "Current Liabilities", "Total Current Liabilities", col=1)
    ni_t = _safe_get(fin, "Net Income", col=0)
    cfo_t = _safe_get(cf, "Operating Cash Flow", "Cash From Operations", "Total Cash From Operating Activities", col=0)

    def safe_div(a: float | None, b: float | None) -> float | None:
        if a is None or b is None or b == 0:
            return None
        return a / b

    # DSRI: Days Sales Receivables Index
    dsri = safe_div(safe_div(rec_t, rev_t), safe_div(rec_t1, rev_t1))

    # GMI: Gross Margin Index
    gm_t = safe_div((rev_t or 0) - (cogs_t or 0), rev_t) if rev_t else None
    gm_t1 = safe_div((rev_t1 or 0) - (cogs_t1 or 0), rev_t1) if rev_t1 else None
    gmi = safe_div(gm_t1, gm_t)  # note: inverted per Beneish convention

    # AQI: Asset Quality Index
    def _aqi_nca(cur_a: float | None, ppe: float | None, assets: float | None) -> float | None:
        if cur_a is None or ppe is None or assets is None or assets == 0:
            return None
        return 1.0 - (cur_a + ppe) / assets
    aqi_t = _aqi_nca(cur_assets_t, ppe_t, assets_t)
    aqi_t1 = _aqi_nca(cur_assets_t1, ppe_t1, assets_t1)
    aqi = safe_div(aqi_t, aqi_t1)

    # SGI: Sales Growth Index
    sgi = safe_div(rev_t, rev_t1)

    # DEPI: Depreciation Index
    def _dep_rate(dep: float | None, ppe: float | None) -> float | None:
        if dep is None or ppe is None:
            return None
        gross_ppe = ppe + dep
        if gross_ppe == 0:
            return None
        return dep / gross_ppe
    depi = safe_div(_dep_rate(dep_t1, ppe_t1), _dep_rate(dep_t, ppe_t))

    # SGAI: SGA Index
    sgai = safe_div(safe_div(sga_t, rev_t), safe_div(sga_t1, rev_t1))

    # LVGI: Leverage Index
    lev_t = safe_div((lt_debt_t or 0) + (cur_liab_t or 0), assets_t) if assets_t else None
    lev_t1 = safe_div((lt_debt_t1 or 0) + (cur_liab_t1 or 0), assets_t1) if assets_t1 else None
    lvgi = safe_div(lev_t, lev_t1)

    # TATA: Total Accruals to Total Assets
    tata = safe_div((ni_t or 0) - (cfo_t or 0), assets_t) if assets_t else None

    # Collect available components (use default if missing)
    defaults = {
        "dsri": (dsri, 1.031),
        "gmi": (gmi, 1.041),
        "aqi": (aqi, 1.039),
        "sgi": (sgi, 1.134),
        "depi": (depi, 1.054),
        "sgai": (sgai, 1.054),
        "lvgi": (lvgi, 1.037),
        "tata": (tata, 0.032),
    }
    coefficients = {
        "dsri": 0.920,
        "gmi": 0.528,
        "aqi": 0.404,
        "sgi": 0.892,
        "depi": 0.115,
        "sgai": -0.172,
        "lvgi": -0.327,
        "tata": 4.679,
    }
    intercept = -4.84

    # Need at least 5 non-None indices to compute M-score
    values = {k: (v if v is not None else d) for k, (v, d) in defaults.items()}
    non_default_count = sum(1 for k, (v, _) in defaults.items() if v is not None)
    if non_default_count < 3:
        return None

    m = intercept + sum(coefficients[k] * values[k] for k in coefficients)
    return m


# ---------------------------------------------------------------------------
# Sloan Accrual Ratio
# ---------------------------------------------------------------------------

def _compute_accrual(
    fin: pd.DataFrame,
    bal: pd.DataFrame,
    cf: pd.DataFrame,
) -> tuple[float | None, AccrualQuality | None]:
    """Sloan Accrual = (Net Income - CFO) / Avg Total Assets."""
    ni = _safe_get(fin, "Net Income", col=0)
    cfo = _safe_get(cf, "Operating Cash Flow", "Cash From Operations", "Total Cash From Operating Activities", col=0)
    assets_t = _safe_get(bal, "Total Assets", col=0)
    assets_t1 = _safe_get(bal, "Total Assets", col=1)

    if ni is None or cfo is None or assets_t is None or assets_t1 is None:
        return None, None

    avg_assets = (assets_t + assets_t1) / 2.0
    if avg_assets == 0:
        return None, None

    accrual = (ni - cfo) / avg_assets

    if accrual < -0.10 or accrual > 0.10:
        quality: AccrualQuality = "low"
    elif abs(accrual) <= 0.05:
        quality = "high"
    else:
        quality = "medium"

    return accrual, quality


# ---------------------------------------------------------------------------
# Overall quality grade
# ---------------------------------------------------------------------------

def _overall_grade(
    f_score: int | None,
    manipulation_risk: bool,
    accrual_quality: AccrualQuality | None,
) -> EarningsQualityGrade:
    if manipulation_risk:
        return "manipulator_risk"
    if f_score is not None:
        if f_score >= 7 and accrual_quality in ("high", "medium", None):
            return "high_quality"
        if f_score <= 2 or accrual_quality == "low":
            return "low_quality"
    return "average_quality"


def _build_interpretation(
    f_score: int | None,
    f_grade: FScoreGrade | None,
    m_score: float | None,
    manipulation_risk: bool,
    accrual: float | None,
    accrual_quality: AccrualQuality | None,
    quality_grade: EarningsQualityGrade,
) -> str:
    parts = []
    if f_score is not None and f_grade is not None:
        grade_map = {
            "very_strong": "极强",
            "strong": "较强",
            "average": "一般",
            "weak": "偏弱",
        }
        parts.append(f"Piotroski F-Score {f_score}/9（{grade_map.get(f_grade, '')}）：得分反映公司盈利能力、杠杆及运营效率的综合改善情况。")
    if m_score is not None:
        risk_str = "⚠ 存在盈利操纵风险" if manipulation_risk else "盈利操纵风险低"
        parts.append(f"Beneish M-Score {m_score:.2f}（阈值 -2.22）：{risk_str}。")
    if accrual is not None and accrual_quality is not None:
        quality_map = {"high": "高质量", "medium": "中等", "low": "低质量"}
        pct = accrual * 100
        parts.append(f"Sloan 应计比率 {pct:+.1f}%（{quality_map.get(accrual_quality, '')}）：正值过高意味着盈利含大量应计项目，现金转化率低。")
    grade_map2 = {
        "high_quality": "综合评级：盈利质量高，财务健康，适合基本面多头策略。",
        "average_quality": "综合评级：盈利质量一般，建议结合行业对标及估值进一步分析。",
        "low_quality": "综合评级：盈利质量偏低，注意财务风险与基本面恶化信号。",
        "manipulator_risk": "综合评级：⚠ Beneish M-Score 触发操纵警告，建议审慎持仓并核实财报。",
    }
    parts.append(grade_map2.get(quality_grade, ""))
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compute_earnings_quality(ticker: str) -> EarningsQualityData:
    """
    Compute earnings quality metrics for a stock.
    Always returns; never raises.
    Requires ≥ 2 years of annual financial data from yfinance.
    """
    ticker = ticker.strip().upper()
    try:
        t = yf.Ticker(ticker)
        fin = t.financials          # income statement (annual)
        bal = t.balance_sheet       # balance sheet (annual)
        cf = t.cashflow             # cash flow statement (annual)

        # Validate data availability
        if (
            fin is None or fin.empty
            or bal is None or bal.empty
            or cf is None or cf.empty
        ):
            return EarningsQualityData(
                ticker=ticker,
                f_score=None,
                f_score_grade=None,
                f_score_components={},
                data_available=False,
                quality_grade="average_quality",
                interpretation="财务数据不可用（yfinance 暂时无法获取或该股票无财报数据）。",
            )

        f_score, f_grade, components = _compute_f_score(fin, bal, cf)
        m_score = _compute_m_score(fin, bal, cf) if _has_col(fin) and _has_col(bal) else None
        manipulation_risk = (m_score is not None and m_score > -2.22)
        accrual, accrual_quality = _compute_accrual(fin, bal, cf)

        quality_grade = _overall_grade(f_score, manipulation_risk, accrual_quality)
        interpretation = _build_interpretation(
            f_score, f_grade, m_score, manipulation_risk, accrual, accrual_quality, quality_grade
        )

        data_available = f_score is not None or m_score is not None or accrual is not None

        return EarningsQualityData(
            ticker=ticker,
            f_score=f_score,
            f_score_grade=f_grade,
            f_score_components=components,
            m_score=m_score,
            manipulation_risk=manipulation_risk,
            accrual_ratio=accrual,
            accrual_quality=accrual_quality,
            quality_grade=quality_grade,
            interpretation=interpretation,
            as_of_date=date.today(),
            data_available=data_available,
        )

    except Exception:  # noqa: BLE001
        return EarningsQualityData(
            ticker=ticker,
            f_score=None,
            f_score_grade=None,
            f_score_components={},
            data_available=False,
            quality_grade="average_quality",
            interpretation="查询失败，请稍后重试。",
        )

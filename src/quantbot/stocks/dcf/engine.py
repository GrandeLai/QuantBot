"""Automated DCF + Monte Carlo valuation engine.

Methodology:
    1. WACC via CAPM (cost of equity) + after-tax cost of debt + capital structure weights.
    2. 5-year FCF projection using last-3-year average FCF × analyst growth (or historical CAGR).
    3. Terminal value via Gordon Growth Model: TV = FCF_n × (1+g) / (WACC - g).
    4. Monte Carlo (1000 sims): vary WACC, FCF growth, terminal growth → fair value distribution.
    5. Output P5 / P50 / P95 fair value per share + margin of safety vs current price.

Alpha signal:
    - margin_of_safety > 30%  → deep_value (historical outperformance: +8-15%/yr)
    - margin_of_safety 10-30% → undervalued
    - Accuracy caveat: DCF is highly sensitive to assumptions; treat as one signal among many.

Data: yfinance free tier (financials, balance_sheet, cash_flow, info).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Literal

import numpy as np
import yfinance as yf
from loguru import logger


# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------

_DEFAULT_RF   = 0.045   # 10-yr US Treasury yield (approx)
_DEFAULT_ERP  = 0.055   # historical equity risk premium
_DEFAULT_TGROW= 0.025   # long-run terminal growth (≈ nominal GDP)
_MC_SIMS      = 1_000
_PROJ_YEARS   = 5

ValuationLabel = Literal[
    "deep_value", "undervalued", "fair", "overvalued", "overheated"
]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class WACCComponents:
    cost_of_equity: float   # CAPM: Rf + β × ERP
    cost_of_debt: float     # interest_expense / total_debt
    tax_rate: float
    debt_weight: float      # D / (D + E)
    equity_weight: float    # E / (D + E)
    wacc: float
    beta: float
    risk_free_rate: float


@dataclass
class DCFResult:
    ticker: str
    current_price: float
    fair_value_p5: float
    fair_value_p50: float
    fair_value_p95: float
    wacc_components: WACCComponents
    base_fcf: float
    npv_fcf: float
    terminal_value_pv: float
    margin_of_safety: float  # (p50 - current) / p50
    valuation: ValuationLabel
    projected_fcfs: list[float]
    as_of_date: date


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe(val: object, default: float = 0.0) -> float:
    try:
        f = float(val)  # type: ignore[arg-type]
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _shares_outstanding(info: dict) -> float | None:
    """Try various keys for shares outstanding."""
    for key in ("sharesOutstanding", "impliedSharesOutstanding", "floatShares"):
        v = info.get(key)
        if v and _safe(v) > 0:
            return _safe(v)
    return None


def _valuation_label(mos: float) -> ValuationLabel:
    if mos > 0.30:
        return "deep_value"
    if mos > 0.10:
        return "undervalued"
    if mos > -0.10:
        return "fair"
    if mos > -0.30:
        return "overvalued"
    return "overheated"


def _npv(cash_flows: list[float], rate: float) -> float:
    return sum(cf / (1 + rate) ** (i + 1) for i, cf in enumerate(cash_flows))


# ---------------------------------------------------------------------------
# WACC computation
# ---------------------------------------------------------------------------

def compute_wacc(
    ticker: str,
    *,
    risk_free_rate: float = _DEFAULT_RF,
    market_premium: float = _DEFAULT_ERP,
) -> WACCComponents | None:
    """Compute WACC for *ticker*.

    Returns None if essential data (beta, market cap, financials) is missing.
    """
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}

        # --- Beta ---
        beta = _safe(info.get("beta"), default=1.0)
        if beta <= 0:
            beta = 1.0  # fallback for negative/zero beta

        cost_of_equity = risk_free_rate + beta * market_premium

        # --- Cost of debt ---
        income = t.financials
        balance = t.balance_sheet
        if income is None or income.empty or balance is None or balance.empty:
            logger.warning(f"[DCF] {ticker}: no financial statements")
            return None

        def _get_latest(df, *keys: str) -> float:
            for k in keys:
                if k in df.index:
                    val = df.loc[k].iloc[0]
                    f = _safe(val)
                    if f != 0.0:
                        return f
            return 0.0

        interest_exp = abs(_get_latest(income, "Interest Expense", "InterestExpense"))
        total_debt   = _get_latest(balance, "Total Debt", "Long Term Debt") or 1.0

        cost_of_debt = interest_exp / total_debt if interest_exp > 0 else 0.04

        # --- Tax rate ---
        pretax = _get_latest(income, "Pretax Income", "Income Before Tax")
        tax    = _get_latest(income, "Tax Provision", "Income Tax Expense")
        if pretax > 0 and tax > 0:
            tax_rate = min(tax / pretax, 0.40)
        else:
            tax_rate = 0.21  # US statutory rate

        # --- Capital structure weights ---
        market_cap = _safe(info.get("marketCap"))
        if market_cap <= 0:
            logger.warning(f"[DCF] {ticker}: no market cap")
            return None

        debt_mkt  = total_debt
        equity_mkt = market_cap
        total_capital = debt_mkt + equity_mkt

        debt_w   = debt_mkt / total_capital
        equity_w = equity_mkt / total_capital

        wacc = equity_w * cost_of_equity + debt_w * cost_of_debt * (1 - tax_rate)

        return WACCComponents(
            cost_of_equity=round(cost_of_equity, 6),
            cost_of_debt=round(cost_of_debt, 6),
            tax_rate=round(tax_rate, 4),
            debt_weight=round(debt_w, 4),
            equity_weight=round(equity_w, 4),
            wacc=round(wacc, 6),
            beta=round(beta, 4),
            risk_free_rate=risk_free_rate,
        )

    except Exception as exc:
        logger.error(f"[DCF/WACC] {ticker}: {exc}")
        return None


# ---------------------------------------------------------------------------
# DCF + Monte Carlo
# ---------------------------------------------------------------------------

def compute_dcf(
    ticker: str,
    *,
    risk_free_rate: float = _DEFAULT_RF,
    market_premium: float = _DEFAULT_ERP,
    terminal_growth: float = _DEFAULT_TGROW,
    projection_years: int = _PROJ_YEARS,
    mc_simulations: int = _MC_SIMS,
) -> DCFResult | None:
    """Run a full DCF valuation with Monte Carlo sensitivity.

    Returns None if essential data is unavailable.
    """
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
        cashflow = t.cashflow

        if cashflow is None or cashflow.empty:
            logger.warning(f"[DCF] {ticker}: no cash flow data")
            return None

        # --- Current price ---
        current_price = _safe(
            info.get("currentPrice") or info.get("regularMarketPrice")
        )
        if current_price <= 0:
            logger.warning(f"[DCF] {ticker}: no current price")
            return None

        # --- Shares outstanding ---
        shares = _shares_outstanding(info)
        if not shares or shares <= 0:
            logger.warning(f"[DCF] {ticker}: no shares outstanding")
            return None

        # --- Historical FCF (last 3 years) ---
        def _get_fcf_series() -> list[float]:
            fcf_vals: list[float] = []
            # Try "Free Cash Flow" key first, else compute OperatingCF - CapEx
            if "Free Cash Flow" in cashflow.index:
                for col in cashflow.columns[:4]:
                    v = _safe(cashflow.loc["Free Cash Flow", col])
                    if v != 0.0:
                        fcf_vals.append(v)
            if not fcf_vals:
                opcf_row = None
                capex_row = None
                for k in ["Operating Cash Flow", "Cash From Operations", "Total Cash From Operations"]:
                    if k in cashflow.index:
                        opcf_row = k
                        break
                for k in ["Capital Expenditure", "Capital Expenditures", "Purchase Of Property Plant And Equipment"]:
                    if k in cashflow.index:
                        capex_row = k
                        break
                if opcf_row and capex_row:
                    for col in cashflow.columns[:4]:
                        opcf  = _safe(cashflow.loc[opcf_row, col])
                        capex = _safe(cashflow.loc[capex_row, col])
                        fcf_vals.append(opcf + capex)  # capex is negative
            return fcf_vals

        fcf_series = _get_fcf_series()
        if not fcf_series:
            logger.warning(f"[DCF] {ticker}: could not extract FCF")
            return None

        # Base FCF = avg of last 3 years (ignore negative outliers for stability)
        positive_fcfs = [f for f in fcf_series[:3] if f > 0]
        if positive_fcfs:
            base_fcf = float(np.mean(positive_fcfs))
        else:
            base_fcf = float(np.mean(fcf_series[:3]))
        if base_fcf == 0:
            return None

        # --- Growth estimate ---
        # Try analyst estimate from yfinance; fallback to historical CAGR
        growth_base: float
        analyst_growth = _safe(info.get("earningsGrowth") or info.get("revenueGrowth"))
        if analyst_growth and 0 < analyst_growth < 0.5:
            growth_base = analyst_growth
        elif len(fcf_series) >= 3 and fcf_series[2] > 0 and fcf_series[0] > 0:
            # CAGR over 3 years
            try:
                growth_base = float((fcf_series[0] / fcf_series[2]) ** (1 / 2) - 1)
                growth_base = max(-0.10, min(0.25, growth_base))  # clip
            except Exception:
                growth_base = 0.05
        else:
            growth_base = 0.05  # conservative default

        # --- WACC ---
        wacc_comp = compute_wacc(
            ticker,
            risk_free_rate=risk_free_rate,
            market_premium=market_premium,
        )
        if wacc_comp is None:
            return None
        wacc_base = wacc_comp.wacc

        # Guard: WACC must be > terminal_growth for stable terminal value
        if wacc_base <= terminal_growth:
            wacc_base = terminal_growth + 0.02

        # --- Base-case projection ---
        def _project_fcfs(g: float) -> list[float]:
            fcfs = []
            cf = base_fcf
            for _ in range(projection_years):
                cf *= (1 + g)
                fcfs.append(cf)
            return fcfs

        projected_fcfs = _project_fcfs(growth_base)

        def _terminal_value(last_fcf: float, g: float, w: float) -> float:
            if w <= g:
                w = g + 0.01
            return last_fcf * (1 + g) / (w - g)

        def _fair_value_per_share(g: float, w: float, tg: float) -> float:
            fcfs = _project_fcfs(g)
            npv_fcf = _npv(fcfs, w)
            tv = _terminal_value(fcfs[-1], tg, w)
            tv_pv = tv / (1 + w) ** projection_years
            total_equity_value = npv_fcf + tv_pv
            return total_equity_value / shares

        # --- Monte Carlo ---
        rng = np.random.default_rng(seed=42)
        wacc_samples   = rng.normal(wacc_base,   0.015, mc_simulations)
        growth_samples = rng.normal(growth_base, 0.030, mc_simulations)
        tgrow_samples  = rng.normal(terminal_growth, 0.005, mc_simulations)

        fair_values: list[float] = []
        for w, g, tg in zip(wacc_samples, growth_samples, tgrow_samples):
            w  = max(0.02, min(0.25, w))
            g  = max(-0.20, min(0.30, g))
            tg = max(0.00, min(0.05, tg))
            try:
                fv = _fair_value_per_share(g, w, tg)
                if math.isfinite(fv) and fv > 0:
                    fair_values.append(fv)
            except Exception:
                pass

        if len(fair_values) < 100:
            logger.warning(f"[DCF] {ticker}: too few valid MC samples ({len(fair_values)})")
            return None

        fv_arr = np.array(fair_values)
        p5  = float(np.percentile(fv_arr, 5))
        p50 = float(np.percentile(fv_arr, 50))
        p95 = float(np.percentile(fv_arr, 95))

        # Base case decomposition
        npv_fcf_base   = _npv(projected_fcfs, wacc_base)
        tv_base        = _terminal_value(projected_fcfs[-1], terminal_growth, wacc_base)
        tv_pv_base     = tv_base / (1 + wacc_base) ** projection_years

        margin_of_safety = (p50 - current_price) / p50 if p50 > 0 else 0.0
        valuation = _valuation_label(margin_of_safety)

        return DCFResult(
            ticker=ticker.upper(),
            current_price=round(current_price, 2),
            fair_value_p5=round(p5, 2),
            fair_value_p50=round(p50, 2),
            fair_value_p95=round(p95, 2),
            wacc_components=wacc_comp,
            base_fcf=round(base_fcf, 0),
            npv_fcf=round(npv_fcf_base, 0),
            terminal_value_pv=round(tv_pv_base, 0),
            margin_of_safety=round(margin_of_safety, 4),
            valuation=valuation,
            projected_fcfs=[round(f, 0) for f in projected_fcfs],
            as_of_date=date.today(),
        )

    except Exception as exc:
        logger.error(f"[DCF] {ticker}: {exc}")
        return None

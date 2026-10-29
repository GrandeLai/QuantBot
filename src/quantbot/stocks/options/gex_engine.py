"""GEX（Gamma Exposure）计算引擎.

公式约定（Dealer-centric）：
  GEX_per_strike = (call_OI - put_OI) × BSM_gamma × contract_multiplier × S²

  - 正 GEX → dealer net long gamma → 价格被钉（mean-reversion）
  - 负 GEX → dealer net short gamma → 波动放大（trending）

contract_multiplier = 100（美股标准期权每张 = 100 股）

关键衍生指标：
  gamma_flip_level   ：距 spot 最近的 GEX 从正转负的行权价
  major_magnet       ：|GEX| 最大的行权价（pin strike）
  high_vol_trigger   ：spot 以下最近的 GEX < 0 的行权价
  net_gex_total      ：所有行权价 GEX 加总（dollar-gamma）

参考文献：
  - SpotGamma "GEX Primer"
  - Benn Eifert, AQR, dealer gamma hedging
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quantbot.stocks.options.chain_provider import OptionsContract

_CONTRACT_MULTIPLIER = 100  # 美股标准：每张期权 = 100 股


@dataclass
class GEXByStrike:
    """单行权价的 GEX 快照."""

    strike: float
    call_oi: int
    put_oi: int
    call_gex: float  # call_OI × gamma × multiplier × S²
    put_gex: float   # put_OI × gamma × multiplier × S²
    net_gex: float   # call_gex - put_gex（dealer convention）
    gamma: float     # BSM gamma（call = put for same strike/expiry）
    dte: float       # 加权平均 DTE（多个到期日聚合时）


@dataclass
class GEXSnapshot:
    """完整 GEX 快照（含所有行权价数据和关键水位）."""

    ticker: str
    spot: float
    snapshot_time: str                      # ISO UTC
    gex_by_strike: list[GEXByStrike] = field(default_factory=list)
    net_gex_total: float = 0.0              # 所有行权价之和（dollar-gamma）
    gamma_flip_level: float | None = None   # GEX 正转负的行权价（距 spot 最近）
    major_magnet: float | None = None       # |GEX| 最大的行权价
    high_vol_trigger: float | None = None   # spot 以下最近的 GEX < 0 行权价


def _bsm_gamma(spot: float, strike: float, dte_days: float, iv: float, r: float) -> float:
    """BSM gamma（N'(d1) / (S × σ × √T)）.

    Returns 0.0 if T or iv is effectively zero (deep expired / zero vol).
    """
    from quantbot.stocks.options.greeks import BlackScholes

    T = dte_days / 365.0
    if T < 1e-6 or iv < 1e-6 or spot <= 0.0 or strike <= 0.0:
        return 0.0
    try:
        bs = BlackScholes(S=spot, K=strike, T=T, r=r, sigma=iv, option_type="call")
        return bs.compute().gamma
    except (ValueError, ZeroDivisionError, OverflowError, math.DomainError if hasattr(math, "DomainError") else Exception):  # type: ignore[attr-defined]
        return 0.0


def compute_gex_snapshot(
    ticker: str,
    spot: float,
    chain: list["OptionsContract"],
    *,
    r: float = 0.05,
    strike_filter_pct: float = 0.30,  # ±30% around spot
) -> GEXSnapshot:
    """从所有期权合约聚合 GEX，返回 GEXSnapshot.

    Args:
        ticker:           标的代码
        spot:             当前现货价格
        chain:            来自 chain_provider 的期权合约列表
        r:                无风险利率（年化）
        strike_filter_pct:只计算 spot±strike_filter_pct 范围内的行权价

    Returns:
        GEXSnapshot（含 gex_by_strike、gamma_flip、magnet、hvt）
    """
    if spot <= 0.0:
        raise ValueError(f"spot must be positive, got {spot}")

    # ── 聚合：per-strike 的 call_oi / put_oi / weighted-avg-dte / weighted-avg-iv ──
    strike_data: dict[float, dict[str, float | int]] = {}
    lo, hi = spot * (1 - strike_filter_pct), spot * (1 + strike_filter_pct)

    for c in chain:
        if c.strike < lo or c.strike > hi:
            continue
        if c.dte < 0:
            continue
        k = c.strike
        if k not in strike_data:
            strike_data[k] = {
                "call_oi": 0,
                "put_oi": 0,
                "call_iv_sum": 0.0,
                "put_iv_sum": 0.0,
                "call_iv_cnt": 0,
                "put_iv_cnt": 0,
                "call_dte_sum": 0.0,
                "put_dte_sum": 0.0,
            }
        d = strike_data[k]
        if c.option_type == "call":
            d["call_oi"] = int(d["call_oi"]) + c.open_interest
            d["call_iv_sum"] = float(d["call_iv_sum"]) + c.implied_volatility * c.open_interest
            d["call_iv_cnt"] = int(d["call_iv_cnt"]) + c.open_interest
            d["call_dte_sum"] = float(d["call_dte_sum"]) + c.dte * c.open_interest
        else:
            d["put_oi"] = int(d["put_oi"]) + c.open_interest
            d["put_iv_sum"] = float(d["put_iv_sum"]) + c.implied_volatility * c.open_interest
            d["put_iv_cnt"] = int(d["put_iv_cnt"]) + c.open_interest
            d["put_dte_sum"] = float(d["put_dte_sum"]) + c.dte * c.open_interest

    if not strike_data:
        return GEXSnapshot(
            ticker=ticker,
            spot=spot,
            snapshot_time=datetime.now(tz=timezone.utc).isoformat(),
        )

    # ── 计算每个行权价的 GEX ──
    gex_list: list[GEXByStrike] = []
    for k in sorted(strike_data):
        d = strike_data[k]
        call_oi = int(d["call_oi"])
        put_oi = int(d["put_oi"])

        # OI-weighted average IV and DTE for gamma calculation
        call_iv_cnt = int(d["call_iv_cnt"])
        put_iv_cnt = int(d["put_iv_cnt"])
        call_iv = (float(d["call_iv_sum"]) / call_iv_cnt) if call_iv_cnt > 0 else 0.0
        put_iv = (float(d["put_iv_sum"]) / put_iv_cnt) if put_iv_cnt > 0 else 0.0
        iv = (call_iv * call_oi + put_iv * put_oi) / (call_oi + put_oi) if (call_oi + put_oi) > 0 else 0.0

        call_dte_sum = float(d["call_dte_sum"])
        put_dte_sum = float(d["put_dte_sum"])
        total_oi_weighted_dte = (call_dte_sum + put_dte_sum)
        total_oi = call_oi + put_oi
        avg_dte = total_oi_weighted_dte / total_oi if total_oi > 0 else 30.0

        if iv <= 0.0:
            continue

        gamma = _bsm_gamma(spot, k, avg_dte, iv, r)
        if gamma <= 0.0:
            continue

        gex_unit = gamma * _CONTRACT_MULTIPLIER * spot * spot
        call_gex = call_oi * gex_unit
        put_gex = put_oi * gex_unit
        net_gex = call_gex - put_gex

        gex_list.append(
            GEXByStrike(
                strike=k,
                call_oi=call_oi,
                put_oi=put_oi,
                call_gex=call_gex,
                put_gex=put_gex,
                net_gex=net_gex,
                gamma=gamma,
                dte=avg_dte,
            )
        )

    if not gex_list:
        return GEXSnapshot(
            ticker=ticker,
            spot=spot,
            snapshot_time=datetime.now(tz=timezone.utc).isoformat(),
        )

    net_gex_total = sum(g.net_gex for g in gex_list)
    gamma_flip = _find_gamma_flip(gex_list, spot)
    magnet = _find_major_magnet(gex_list)
    hvt = _find_high_vol_trigger(gex_list, spot)

    return GEXSnapshot(
        ticker=ticker,
        spot=spot,
        snapshot_time=datetime.now(tz=timezone.utc).isoformat(),
        gex_by_strike=gex_list,
        net_gex_total=net_gex_total,
        gamma_flip_level=gamma_flip,
        major_magnet=magnet,
        high_vol_trigger=hvt,
    )


# ── helper finders ────────────────────────────────────────────────────────────


def _find_gamma_flip(gex_list: list[GEXByStrike], spot: float) -> float | None:
    """Find the strike closest to spot where per-strike net_gex changes sign.

    Scans sorted strikes and finds adjacent pairs where net_gex transitions
    from positive to negative (or vice versa). Returns the sign-change strike
    that is closest to current spot price.
    """
    if not gex_list:
        return None

    strikes_asc = sorted(gex_list, key=lambda g: g.strike)
    closest: float | None = None
    closest_dist = float("inf")

    for i in range(1, len(strikes_asc)):
        prev_g = strikes_asc[i - 1]
        curr_g = strikes_asc[i]
        # Sign change between adjacent strikes
        if (prev_g.net_gex >= 0) != (curr_g.net_gex >= 0):
            # The flip is at the boundary — pick the strike closer to spot
            for candidate in (prev_g.strike, curr_g.strike):
                dist = abs(candidate - spot)
                if dist < closest_dist:
                    closest_dist = dist
                    closest = candidate

    return closest


def _find_major_magnet(gex_list: list[GEXByStrike]) -> float | None:
    """行权价中 |net_gex| 最大（pin strike）."""
    if not gex_list:
        return None
    return max(gex_list, key=lambda g: abs(g.net_gex)).strike


def _find_high_vol_trigger(gex_list: list[GEXByStrike], spot: float) -> float | None:
    """spot 以下 net_gex < 0 的行权价中离 spot 最近的（下行加速触发线）."""
    candidates = [g for g in gex_list if g.strike < spot and g.net_gex < 0]
    if not candidates:
        return None
    return max(candidates, key=lambda g: g.strike).strike  # highest below spot

"""Tests for GEX engine (phaseF.options-gex-engine)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from quantbot.stocks.options.chain_provider import OptionsContract
from quantbot.stocks.options.gex_engine import (
    GEXByStrike,
    _find_gamma_flip,
    _find_high_vol_trigger,
    _find_major_magnet,
    compute_gex_snapshot,
)


# ---------- helpers ----------------------------------------------------------

TODAY = datetime.now(tz=timezone.utc).date()


def _contract(
    strike: float,
    option_type: str,
    oi: int = 1000,
    iv: float = 0.20,
    dte: int = 21,
) -> OptionsContract:
    return OptionsContract(
        ticker="SPY",
        expiry=TODAY,
        strike=strike,
        option_type=option_type,  # type: ignore[arg-type]
        open_interest=oi,
        implied_volatility=iv,
        last_price=1.0,
        bid=0.9,
        ask=1.1,
        volume=100,
        dte=dte,
    )


def _build_chain(spot: float = 500.0, n: int = 5) -> list[OptionsContract]:
    """Build a symmetric call + put chain around spot."""
    step = 5.0
    contracts: list[OptionsContract] = []
    for i in range(-n, n + 1):
        k = round(spot + i * step, 1)
        contracts.append(_contract(k, "call"))
        contracts.append(_contract(k, "put"))
    return contracts


# ---------- unit tests -------------------------------------------------------


class TestComputeGEXSnapshot:
    def test_happy_path_returns_snapshot(self) -> None:
        chain = _build_chain(500.0, n=5)
        snap = compute_gex_snapshot("SPY", 500.0, chain, r=0.05)
        assert snap.ticker == "SPY"
        assert snap.spot == 500.0
        assert len(snap.gex_by_strike) > 0

    def test_net_gex_total_is_sum_of_strikes(self) -> None:
        chain = _build_chain(500.0, n=3)
        snap = compute_gex_snapshot("SPY", 500.0, chain, r=0.05)
        expected = sum(g.net_gex for g in snap.gex_by_strike)
        assert abs(snap.net_gex_total - expected) < 1e-6

    def test_atm_strike_present(self) -> None:
        chain = _build_chain(500.0, n=3)
        snap = compute_gex_snapshot("SPY", 500.0, chain, r=0.05)
        strikes = {g.strike for g in snap.gex_by_strike}
        assert 500.0 in strikes

    def test_invalid_spot_raises(self) -> None:
        chain = _build_chain(500.0, n=2)
        with pytest.raises(ValueError, match="spot must be positive"):
            compute_gex_snapshot("SPY", 0.0, chain)

    def test_empty_chain_returns_empty_snapshot(self) -> None:
        snap = compute_gex_snapshot("SPY", 500.0, [])
        assert snap.gex_by_strike == []
        assert snap.net_gex_total == 0.0

    def test_call_heavy_chain_positive_gex(self) -> None:
        """More call OI than put OI → positive net GEX (dealer long gamma)."""
        spot = 500.0
        k = 500.0
        chain = [
            _contract(k, "call", oi=5000),
            _contract(k, "put", oi=500),
        ]
        snap = compute_gex_snapshot("SPY", spot, chain, r=0.05)
        assert snap.net_gex_total > 0

    def test_put_heavy_chain_negative_gex(self) -> None:
        """More put OI → negative net GEX."""
        spot = 500.0
        k = 500.0
        chain = [
            _contract(k, "call", oi=500),
            _contract(k, "put", oi=5000),
        ]
        snap = compute_gex_snapshot("SPY", spot, chain, r=0.05)
        assert snap.net_gex_total < 0

    def test_out_of_range_strikes_filtered(self) -> None:
        """Strikes > ±30% of spot are excluded."""
        spot = 500.0
        in_chain = [
            _contract(500.0, "call"),
            _contract(500.0, "put"),
        ]
        out_chain = [
            _contract(800.0, "call"),   # +60% → excluded
            _contract(200.0, "put"),    # -60% → excluded
        ]
        snap_in = compute_gex_snapshot("SPY", spot, in_chain + out_chain)
        snap_only_in = compute_gex_snapshot("SPY", spot, in_chain)
        assert snap_in.net_gex_total == pytest.approx(snap_only_in.net_gex_total, rel=1e-6)


class TestFindMajorMagnet:
    def test_returns_highest_abs_gex_strike(self) -> None:
        gex_list = [
            GEXByStrike(490.0, 100, 100, 1000.0, 1000.0, 0.0, 0.01, 21),
            GEXByStrike(500.0, 500, 100, 5000.0, 1000.0, 4000.0, 0.01, 21),  # highest |GEX|
            GEXByStrike(510.0, 100, 200, 1000.0, 2000.0, -1000.0, 0.01, 21),
        ]
        assert _find_major_magnet(gex_list) == 500.0

    def test_empty_returns_none(self) -> None:
        assert _find_major_magnet([]) is None


class TestFindGammaFlip:
    def test_sign_change_detected(self) -> None:
        # Positive GEX at low strikes → negative at high → flip around 500
        gex_list = [
            GEXByStrike(490.0, 0, 0, 0.0, 0.0, 1000.0, 0.01, 21),
            GEXByStrike(495.0, 0, 0, 0.0, 0.0, 2000.0, 0.01, 21),
            GEXByStrike(500.0, 0, 0, 0.0, 0.0, -500.0, 0.01, 21),   # flip here
            GEXByStrike(505.0, 0, 0, 0.0, 0.0, -1000.0, 0.01, 21),
        ]
        flip = _find_gamma_flip(gex_list, spot=499.0)
        assert flip == 500.0

    def test_no_flip_returns_none(self) -> None:
        # All positive GEX — no flip
        gex_list = [
            GEXByStrike(490.0, 0, 0, 0.0, 0.0, 1000.0, 0.01, 21),
            GEXByStrike(500.0, 0, 0, 0.0, 0.0, 500.0, 0.01, 21),
        ]
        assert _find_gamma_flip(gex_list, spot=495.0) is None


class TestFindHighVolTrigger:
    def test_returns_nearest_negative_below_spot(self) -> None:
        gex_list = [
            GEXByStrike(480.0, 0, 0, 0.0, 0.0, -800.0, 0.01, 21),
            GEXByStrike(490.0, 0, 0, 0.0, 0.0, -200.0, 0.01, 21),   # nearest below spot
            GEXByStrike(500.0, 0, 0, 0.0, 0.0, 1000.0, 0.01, 21),
            GEXByStrike(510.0, 0, 0, 0.0, 0.0, -300.0, 0.01, 21),   # above spot → excluded
        ]
        hvt = _find_high_vol_trigger(gex_list, spot=498.0)
        assert hvt == 490.0

    def test_no_negative_below_spot_returns_none(self) -> None:
        gex_list = [
            GEXByStrike(490.0, 0, 0, 0.0, 0.0, 800.0, 0.01, 21),
            GEXByStrike(500.0, 0, 0, 0.0, 0.0, 900.0, 0.01, 21),
        ]
        assert _find_high_vol_trigger(gex_list, spot=498.0) is None

"""API tests for /options/gex/* endpoints (phaseF.options-gex-api)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import quantbot.stocks.api.gex as gex_module
from quantbot.web.app import app
from quantbot.stocks.options.gex_engine import GEXByStrike, GEXSnapshot

client = TestClient(app)

# ---------- helpers ----------------------------------------------------------

_SNAPSHOT_TIME = datetime.now(tz=timezone.utc).isoformat()


def _fake_snapshot(ticker: str = "SPY", spot: float = 500.0) -> GEXSnapshot:
    gex_list = [
        GEXByStrike(490.0, 1000, 2000, 2e6, 4e6, -2e6, 0.005, 21.0),
        GEXByStrike(500.0, 3000, 1000, 6e6, 2e6, 4e6, 0.006, 21.0),
        GEXByStrike(510.0, 2000, 500, 4e6, 1e6, 3e6, 0.004, 21.0),
    ]
    return GEXSnapshot(
        ticker=ticker,
        spot=spot,
        snapshot_time=_SNAPSHOT_TIME,
        gex_by_strike=gex_list,
        net_gex_total=5e6,
        gamma_flip_level=495.0,
        major_magnet=500.0,
        high_vol_trigger=490.0,
    )


# ---------- /options/gex/snapshot --------------------------------------------


class TestGEXSnapshot:
    def test_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_build(ticker: str, max_dte: int, min_oi: int, r: float) -> GEXSnapshot:
            return _fake_snapshot(ticker)

        monkeypatch.setattr(gex_module, "_build_snapshot", fake_build)
        r = client.get("/options/gex/snapshot?ticker=SPY")
        assert r.status_code == 200
        body = r.json()
        assert body["ticker"] == "SPY"
        assert body["spot"] == 500.0
        assert "gex_by_strike" in body
        assert len(body["gex_by_strike"]) == 3
        assert "net_gex_total" in body
        assert "gamma_flip_level" in body
        assert "major_magnet" in body
        assert "high_vol_trigger" in body

    def test_gex_by_strike_schema(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_build(ticker: str, max_dte: int, min_oi: int, r: float) -> GEXSnapshot:
            return _fake_snapshot()

        monkeypatch.setattr(gex_module, "_build_snapshot", fake_build)
        r = client.get("/options/gex/snapshot")
        body = r.json()
        first = body["gex_by_strike"][0]
        for key in ["strike", "call_oi", "put_oi", "call_gex", "put_gex", "net_gex", "gamma"]:
            assert key in first, f"missing key: {key}"

    def test_api_alias_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_build(ticker: str, max_dte: int, min_oi: int, r: float) -> GEXSnapshot:
            return _fake_snapshot()

        monkeypatch.setattr(gex_module, "_build_snapshot", fake_build)
        r = client.get("/api/options/gex/snapshot?ticker=QQQ")
        assert r.status_code == 200

    def test_ticker_uppercased(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[str] = []

        async def fake_build(ticker: str, max_dte: int, min_oi: int, r: float) -> GEXSnapshot:
            captured.append(ticker)
            return _fake_snapshot(ticker)

        monkeypatch.setattr(gex_module, "_build_snapshot", fake_build)
        client.get("/options/gex/snapshot?ticker=spy")
        assert captured == ["SPY"]


# ---------- /options/gex/levels ----------------------------------------------


class TestGEXLevels:
    def test_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_build(ticker: str, max_dte: int, min_oi: int, r: float) -> GEXSnapshot:
            return _fake_snapshot()

        monkeypatch.setattr(gex_module, "_build_snapshot", fake_build)
        r = client.get("/options/gex/levels?ticker=SPY")
        assert r.status_code == 200
        body = r.json()
        assert "gamma_flip_level" in body
        assert "major_magnet" in body
        assert "high_vol_trigger" in body
        assert "net_gex_total" in body
        assert "spot" in body
        # No gex_by_strike in levels
        assert "gex_by_strike" not in body

    def test_api_alias_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_build(ticker: str, max_dte: int, min_oi: int, r: float) -> GEXSnapshot:
            return _fake_snapshot()

        monkeypatch.setattr(gex_module, "_build_snapshot", fake_build)
        r = client.get("/api/options/gex/levels")
        assert r.status_code == 200

    def test_levels_values_match_snapshot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        snap = _fake_snapshot()

        async def fake_build(ticker: str, max_dte: int, min_oi: int, r: float) -> GEXSnapshot:
            return snap

        monkeypatch.setattr(gex_module, "_build_snapshot", fake_build)
        r = client.get("/options/gex/levels")
        body = r.json()
        assert body["gamma_flip_level"] == snap.gamma_flip_level
        assert body["major_magnet"] == snap.major_magnet
        assert body["high_vol_trigger"] == snap.high_vol_trigger

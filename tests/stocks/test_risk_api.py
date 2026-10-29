"""API tests for /risk/* endpoints (phaseF.1.4)."""
from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from quantbot.web.app import app

client = TestClient(app)


@pytest.fixture
def normal_returns() -> list[float]:
    rng = np.random.default_rng(42)
    return rng.normal(0.0005, 0.012, size=400).tolist()


# --- /risk/kelly -------------------------------------------------------------


class TestKellyEndpoint:
    def test_binary_mode_happy_path(self) -> None:
        r = client.post(
            "/risk/kelly",
            json={"mode": "binary", "win_rate": 0.6, "payoff_ratio": 1.0},
        )
        assert r.status_code == 200
        body = r.json()
        # f* = 0.6 - 0.4 = 0.20
        assert abs(body["full_kelly"] - 0.20) < 1e-9
        assert abs(body["fractional_kelly"] - 0.05) < 1e-9
        assert abs(body["capped_kelly"] - 0.05) < 1e-9
        assert body["mode"] == "binary"

    def test_binary_mode_capped(self) -> None:
        r = client.post(
            "/risk/kelly",
            json={
                "mode": "binary",
                "win_rate": 0.9,
                "payoff_ratio": 5.0,
                "fraction": 1.0,
                "cap": 0.25,
            },
        )
        assert r.status_code == 200
        body = r.json()
        # f* = 0.9 - 0.1/5 = 0.88 → capped 0.25
        assert abs(body["full_kelly"] - 0.88) < 1e-6
        assert body["capped_kelly"] == 0.25

    def test_returns_mode_happy_path(self, normal_returns: list[float]) -> None:
        r = client.post("/risk/kelly", json={"mode": "returns", "returns": normal_returns})
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "returns"
        assert body["full_kelly"] >= 0.0
        assert body["capped_kelly"] <= 0.25

    def test_binary_missing_fields(self) -> None:
        r = client.post("/risk/kelly", json={"mode": "binary"})
        assert r.status_code == 400

    def test_returns_empty_returns(self) -> None:
        r = client.post("/risk/kelly", json={"mode": "returns", "returns": []})
        assert r.status_code == 400

    def test_invalid_win_rate(self) -> None:
        r = client.post(
            "/risk/kelly",
            json={"mode": "binary", "win_rate": 1.5, "payoff_ratio": 1.0},
        )
        assert r.status_code == 400


# --- /risk/vol-target --------------------------------------------------------


class TestVolTargetEndpoint:
    def test_happy_path(self, normal_returns: list[float]) -> None:
        r = client.post("/risk/vol-target", json={"returns": normal_returns, "target_vol": 0.15})
        assert r.status_code == 200
        body = r.json()
        for k in ("realized_vol", "target_vol", "scale_factor", "regime"):
            assert k in body
        assert body["regime"] in {"low", "normal", "high", "crisis"}

    def test_too_few_samples(self) -> None:
        r = client.post("/risk/vol-target", json={"returns": [0.01]})
        assert r.status_code == 400


# --- /risk/sharpe-decay ------------------------------------------------------


class TestSharpeDecayEndpoint:
    def test_happy_path(self) -> None:
        rng = np.random.default_rng(11)
        returns = rng.normal(0.001, 0.01, size=400).tolist()
        r = client.post(
            "/risk/sharpe-decay",
            json={"returns": returns, "recent_window": 63, "baseline_window": 252},
        )
        assert r.status_code == 200
        body = r.json()
        for k in ("recent_sharpe", "baseline_mean", "baseline_std", "z_score", "alert_level"):
            assert k in body
        assert body["alert_level"] in {"green", "yellow", "red"}

    def test_too_few_samples(self) -> None:
        r = client.post("/risk/sharpe-decay", json={"returns": [0.01] * 100})
        assert r.status_code == 400


# --- /risk/var ---------------------------------------------------------------


class TestVarEndpoint:
    def test_historical_default(self, normal_returns: list[float]) -> None:
        r = client.post("/risk/var", json={"returns": normal_returns})
        assert r.status_code == 200
        body = r.json()
        assert body["method"] == "historical"
        assert body["var_95"] > 0.0
        assert body["cvar_95"] >= body["var_95"]

    def test_method_both(self, normal_returns: list[float]) -> None:
        r = client.post("/risk/var", json={"returns": normal_returns, "method": "both"})
        assert r.status_code == 200
        body = r.json()
        assert "parametric_var_95" in body
        assert "var_95" in body

    def test_empty_confidences(self, normal_returns: list[float]) -> None:
        r = client.post("/risk/var", json={"returns": normal_returns, "confidences": []})
        assert r.status_code == 400

    def test_too_few_samples(self) -> None:
        r = client.post("/risk/var", json={"returns": [0.01] * 10})
        assert r.status_code == 400


# --- /risk/summary -----------------------------------------------------------


class TestSummaryEndpoint:
    def test_happy_path_full_decay(self) -> None:
        rng = np.random.default_rng(50)
        returns = rng.normal(0.0005, 0.011, size=500).tolist()
        r = client.post("/risk/summary", json={"returns": returns})
        assert r.status_code == 200
        body = r.json()
        assert "vol_target" in body
        assert "sharpe_decay" in body
        assert "var" in body
        assert body["sharpe_decay"] is not None  # 500 >= 63+252+10
        assert body["n_samples"] == 500

    def test_short_history_skips_decay(self, normal_returns: list[float]) -> None:
        # 400 个样本 < 63+252+10=325 ❌ 实际 400 > 325，应该跑出 decay
        # 用更短的样本测试 decay 退化
        rng = np.random.default_rng(60)
        short = rng.normal(0.0, 0.01, size=200).tolist()
        r = client.post("/risk/summary", json={"returns": short})
        assert r.status_code == 200
        body = r.json()
        # 200 < 63+252+10=325, decay 应为 None
        assert body["sharpe_decay"] is None
        # vol_target 和 var 仍可正常工作（min 30 / 2 个样本）
        assert body["vol_target"] is not None
        assert body["var"] is not None

    def test_too_few_for_anything(self) -> None:
        r = client.post("/risk/summary", json={"returns": [0.01] * 10})
        # vol_target 需要 ≥ 2，OK；var 需要 ≥ 30，会 raise
        assert r.status_code == 400

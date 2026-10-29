"""Unit tests for crypto whale / CEX inflow engine."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from quantbot.crypto.crypto_whale.engine import (
    CEXInflowData,
    _compute_pressure_score,
    _pressure_signal,
    _wei_to_eth,
    compute_cex_inflow,
    fetch_recent_whale_transfers,
)


# ---------------------------------------------------------------------------
# _wei_to_eth
# ---------------------------------------------------------------------------


class TestWeiToEth:
    def test_basic_conversion(self):
        # 1 ETH = 1e18 wei
        assert _wei_to_eth("1000000000000000000") == pytest.approx(1.0)

    def test_100_eth(self):
        assert _wei_to_eth("100000000000000000000") == pytest.approx(100.0)

    def test_invalid_returns_zero(self):
        assert _wei_to_eth("invalid") == 0.0

    def test_empty_returns_zero(self):
        assert _wei_to_eth("") == 0.0


# ---------------------------------------------------------------------------
# _compute_pressure_score
# ---------------------------------------------------------------------------


class TestComputePressureScore:
    def test_equal_inflow_outflow_is_neutral(self):
        assert _compute_pressure_score(100.0, 100.0) == pytest.approx(0.5)

    def test_all_inflow_is_one(self):
        assert _compute_pressure_score(500.0, 0.0) == pytest.approx(1.0)

    def test_all_outflow_is_zero(self):
        assert _compute_pressure_score(0.0, 500.0) == pytest.approx(0.0)

    def test_zero_zero_is_neutral(self):
        assert _compute_pressure_score(0.0, 0.0) == pytest.approx(0.5)

    def test_score_in_range(self):
        for inf in [0, 100, 500, 1000]:
            for out in [0, 100, 500, 1000]:
                s = _compute_pressure_score(float(inf), float(out))
                assert 0.0 <= s <= 1.0


# ---------------------------------------------------------------------------
# _pressure_signal
# ---------------------------------------------------------------------------


class TestPressureSignal:
    def test_heavy_inflow(self):
        assert _pressure_signal(0.80) == "heavy_inflow"

    def test_elevated_inflow(self):
        assert _pressure_signal(0.65) == "elevated_inflow"

    def test_neutral(self):
        assert _pressure_signal(0.50) == "neutral"

    def test_accumulation(self):
        assert _pressure_signal(0.35) == "accumulation"

    def test_heavy_accumulation(self):
        assert _pressure_signal(0.20) == "heavy_accumulation"


# ---------------------------------------------------------------------------
# compute_cex_inflow — no API key
# ---------------------------------------------------------------------------


class TestComputeCEXInflowNoKey:
    def test_returns_data_object(self):
        with patch.dict("os.environ", {}, clear=True):
            # Ensure key is absent
            import os
            os.environ.pop("ETHERSCAN_API_KEY", None)
            result = compute_cex_inflow()
        assert isinstance(result, CEXInflowData)

    def test_api_key_missing_flag(self):
        with patch.dict("os.environ", {}, clear=True):
            import os
            os.environ.pop("ETHERSCAN_API_KEY", None)
            result = compute_cex_inflow()
        assert result.api_key_missing is True

    def test_zero_flows_when_no_key(self):
        with patch.dict("os.environ", {}, clear=True):
            import os
            os.environ.pop("ETHERSCAN_API_KEY", None)
            result = compute_cex_inflow()
        assert result.inflow_eth == 0.0
        assert result.outflow_eth == 0.0
        assert result.pressure_score == pytest.approx(0.5)

    def test_neutral_signal_when_no_key(self):
        with patch.dict("os.environ", {}, clear=True):
            import os
            os.environ.pop("ETHERSCAN_API_KEY", None)
            result = compute_cex_inflow()
        assert result.signal == "neutral"


# ---------------------------------------------------------------------------
# compute_cex_inflow — with mocked HTTP
# ---------------------------------------------------------------------------

def _make_tx(
    tx_hash: str,
    from_addr: str,
    to_addr: str,
    value_eth: float,
    timestamp: int,
    is_error: str = "0",
) -> dict:
    return {
        "hash": tx_hash,
        "from": from_addr,
        "to": to_addr,
        "value": str(int(value_eth * 1e18)),
        "timeStamp": str(timestamp),
        "isError": is_error,
    }


_BINANCE_ADDR = "0x3f5ce5fbfe3e9af3971dd833d26ba9b5c936f0be"
_EXTERNAL = "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
_NOW_TS = 1714340000  # fixed fake "now"


def _mock_txlist_response(txs: list[dict]) -> dict:
    return {"status": "1", "result": txs}


class TestComputeCEXInflowMocked:
    @patch("quantbot.crypto.crypto_whale.engine.time")
    @patch("quantbot.crypto.crypto_whale.engine._get_eth_price", return_value=3000.0)
    @patch("quantbot.crypto.crypto_whale.engine._fetch_txlist")
    def test_inflow_detected(self, mock_fetch, mock_price, mock_time):
        mock_time.time.return_value = _NOW_TS
        # One inflow tx: external → Binance, 200 ETH
        mock_fetch.return_value = [
            _make_tx("0xaaa", _EXTERNAL, _BINANCE_ADDR, 200.0, _NOW_TS - 3600)
        ]
        with patch.dict("os.environ", {"ETHERSCAN_API_KEY": "fake_key"}):
            result = compute_cex_inflow(hours=24, min_eth=100.0)
        assert result.inflow_eth > 0
        assert result.transfer_count > 0

    @patch("quantbot.crypto.crypto_whale.engine.time")
    @patch("quantbot.crypto.crypto_whale.engine._get_eth_price", return_value=3000.0)
    @patch("quantbot.crypto.crypto_whale.engine._fetch_txlist")
    def test_outflow_detected(self, mock_fetch, mock_price, mock_time):
        mock_time.time.return_value = _NOW_TS
        # One outflow tx: Binance → external, 300 ETH
        mock_fetch.return_value = [
            _make_tx("0xbbb", _BINANCE_ADDR, _EXTERNAL, 300.0, _NOW_TS - 1800)
        ]
        with patch.dict("os.environ", {"ETHERSCAN_API_KEY": "fake_key"}):
            result = compute_cex_inflow(hours=24, min_eth=100.0)
        assert result.outflow_eth > 0

    @patch("quantbot.crypto.crypto_whale.engine.time")
    @patch("quantbot.crypto.crypto_whale.engine._get_eth_price", return_value=3000.0)
    @patch("quantbot.crypto.crypto_whale.engine._fetch_txlist")
    def test_small_tx_filtered(self, mock_fetch, mock_price, mock_time):
        mock_time.time.return_value = _NOW_TS
        # Tx below min_eth threshold
        mock_fetch.return_value = [
            _make_tx("0xccc", _EXTERNAL, _BINANCE_ADDR, 50.0, _NOW_TS - 3600)
        ]
        with patch.dict("os.environ", {"ETHERSCAN_API_KEY": "fake_key"}):
            result = compute_cex_inflow(hours=24, min_eth=100.0)
        assert result.inflow_eth == pytest.approx(0.0)

    @patch("quantbot.crypto.crypto_whale.engine.time")
    @patch("quantbot.crypto.crypto_whale.engine._get_eth_price", return_value=3000.0)
    @patch("quantbot.crypto.crypto_whale.engine._fetch_txlist")
    def test_signal_is_valid_literal(self, mock_fetch, mock_price, mock_time):
        mock_time.time.return_value = _NOW_TS
        mock_fetch.return_value = []
        with patch.dict("os.environ", {"ETHERSCAN_API_KEY": "fake_key"}):
            result = compute_cex_inflow()
        assert result.signal in {
            "heavy_inflow", "elevated_inflow", "neutral",
            "accumulation", "heavy_accumulation",
        }

    @patch("quantbot.crypto.crypto_whale.engine.time")
    @patch("quantbot.crypto.crypto_whale.engine._get_eth_price", return_value=None)
    @patch("quantbot.crypto.crypto_whale.engine._fetch_txlist")
    def test_inflow_usd_none_when_no_price(self, mock_fetch, mock_price, mock_time):
        mock_time.time.return_value = _NOW_TS
        mock_fetch.return_value = []
        with patch.dict("os.environ", {"ETHERSCAN_API_KEY": "fake_key"}):
            result = compute_cex_inflow()
        assert result.inflow_usd is None

    @patch("quantbot.crypto.crypto_whale.engine.time")
    @patch("quantbot.crypto.crypto_whale.engine._get_eth_price", return_value=3000.0)
    @patch("quantbot.crypto.crypto_whale.engine._fetch_txlist")
    def test_fetch_recent_returns_list(self, mock_fetch, mock_price, mock_time):
        mock_time.time.return_value = _NOW_TS
        mock_fetch.return_value = [
            _make_tx("0xddd", _EXTERNAL, _BINANCE_ADDR, 150.0, _NOW_TS - 7200)
        ]
        with patch.dict("os.environ", {"ETHERSCAN_API_KEY": "fake_key"}):
            transfers = fetch_recent_whale_transfers(hours=24, min_eth=100.0, limit=10)
        assert isinstance(transfers, list)

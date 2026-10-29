"""Tests for options chain provider (phaseF.options-gex-provider)."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from quantbot.stocks.options.chain_provider import fetch_chain_yfinance


# ---------- helpers ----------------------------------------------------------


def _make_calls_df(strikes: list[float], oi: list[int], iv: list[float]) -> pd.DataFrame:
    """Build a minimal yfinance-style calls DataFrame."""
    return pd.DataFrame(
        {
            "strike": strikes,
            "openInterest": oi,
            "impliedVolatility": iv,
            "lastPrice": [1.0] * len(strikes),
            "bid": [0.9] * len(strikes),
            "ask": [1.1] * len(strikes),
            "volume": [100] * len(strikes),
        }
    )


def _make_puts_df(strikes: list[float], oi: list[int], iv: list[float]) -> pd.DataFrame:
    return _make_calls_df(strikes, oi, iv)  # same schema


class _FakeChain:
    def __init__(self, calls: pd.DataFrame, puts: pd.DataFrame) -> None:
        self.calls = calls
        self.puts = puts


class _FakeYFTicker:
    """Mock for yfinance.Ticker with .options and .option_chain()."""

    def __init__(
        self,
        options: tuple[str, ...],
        chains: dict[str, _FakeChain],
    ) -> None:
        self.options = options
        self._chains = chains

    def option_chain(self, date_str: str) -> _FakeChain:
        return self._chains[date_str]


# ---------- tests ------------------------------------------------------------


class TestFetchChainYfinance:
    def _make_expiry(self, days_ahead: int) -> str:
        from datetime import timedelta

        d = datetime.now(tz=timezone.utc).date() + timedelta(days=days_ahead)
        return d.isoformat()

    def test_happy_path_returns_contracts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        expiry = self._make_expiry(20)
        calls = _make_calls_df([580.0, 590.0], [1000, 500], [0.20, 0.22])
        puts = _make_puts_df([570.0, 580.0], [800, 600], [0.21, 0.19])
        fake_ticker = _FakeYFTicker(options=(expiry,), chains={expiry: _FakeChain(calls, puts)})

        import yfinance as yf

        monkeypatch.setattr(yf, "Ticker", lambda ticker: fake_ticker)

        result = fetch_chain_yfinance("SPY")
        assert len(result) == 4  # 2 calls + 2 puts
        types = {c.option_type for c in result}
        assert types == {"call", "put"}
        for c in result:
            assert c.ticker == "SPY"
            assert c.implied_volatility > 0
            assert c.open_interest > 0

    def test_max_dte_filters_far_expiry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        near = self._make_expiry(10)
        far = self._make_expiry(60)
        calls = _make_calls_df([580.0], [1000], [0.20])
        puts = _make_puts_df([580.0], [800], [0.21])
        fake_ticker = _FakeYFTicker(
            options=(near, far),
            chains={
                near: _FakeChain(calls, puts),
                far: _FakeChain(calls, puts),
            },
        )

        import yfinance as yf

        monkeypatch.setattr(yf, "Ticker", lambda ticker: fake_ticker)

        result = fetch_chain_yfinance("SPY", max_dte=45)
        # Only near expiry (10 DTE) should be included
        assert all(c.dte <= 45 for c in result)
        # far expiry (60 DTE) excluded
        assert all(c.dte != 60 for c in result)

    def test_min_oi_filter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        expiry = self._make_expiry(15)
        calls = _make_calls_df([580.0, 590.0], [5, 200], [0.20, 0.22])
        puts = _make_puts_df([570.0], [300], [0.21])
        fake_ticker = _FakeYFTicker(options=(expiry,), chains={expiry: _FakeChain(calls, puts)})

        import yfinance as yf

        monkeypatch.setattr(yf, "Ticker", lambda ticker: fake_ticker)

        result = fetch_chain_yfinance("SPY", min_oi=100)
        assert all(c.open_interest >= 100 for c in result)
        assert len(result) == 2  # strike 580 call (OI=5) excluded

    def test_zero_iv_excluded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        expiry = self._make_expiry(15)
        calls = _make_calls_df([580.0, 590.0], [1000, 500], [0.0, 0.22])  # 580 iv=0
        puts = _make_puts_df([], [], [])
        fake_ticker = _FakeYFTicker(options=(expiry,), chains={expiry: _FakeChain(calls, puts)})

        import yfinance as yf

        monkeypatch.setattr(yf, "Ticker", lambda ticker: fake_ticker)

        result = fetch_chain_yfinance("SPY")
        assert all(c.implied_volatility > 0 for c in result)
        assert len(result) == 1

    def test_empty_options_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_ticker = _FakeYFTicker(options=(), chains={})

        import yfinance as yf

        monkeypatch.setattr(yf, "Ticker", lambda ticker: fake_ticker)

        with pytest.raises(RuntimeError, match="没有可用期权数据"):
            fetch_chain_yfinance("SPY")

    def test_ticker_uppercased(self, monkeypatch: pytest.MonkeyPatch) -> None:
        expiry = self._make_expiry(10)
        calls = _make_calls_df([580.0], [1000], [0.20])
        puts = _make_puts_df([580.0], [800], [0.21])
        fake_ticker = _FakeYFTicker(options=(expiry,), chains={expiry: _FakeChain(calls, puts)})

        import yfinance as yf

        monkeypatch.setattr(yf, "Ticker", lambda ticker: fake_ticker)

        result = fetch_chain_yfinance("spy")
        assert all(c.ticker == "SPY" for c in result)

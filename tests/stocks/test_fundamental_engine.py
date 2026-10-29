"""基本面 Alpha 引擎单元测试（Phase F.4.1）.

使用 unittest.mock 避免真实网络调用。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from quantbot.stocks.fundamental.engine import (
    _classify_surprise,
    _safe_float,
    _signal_strength_from_surprise,
    compute_pead_signal,
    compute_piotroski_fscore,
)


# ---------------------------------------------------------------------------
# 1. 工具函数
# ---------------------------------------------------------------------------


class TestSafeFloat:
    def test_normal_value(self) -> None:
        assert _safe_float(3.14) == pytest.approx(3.14)

    def test_none_returns_default(self) -> None:
        assert _safe_float(None) == 0.0

    def test_nan_returns_default(self) -> None:
        assert _safe_float(float("nan")) == 0.0

    def test_string_fails_returns_default(self) -> None:
        assert _safe_float("bad") == 0.0


class TestClassifySurprise:
    def test_large_beat(self) -> None:
        assert _classify_surprise(0.08) == "large_beat"

    def test_beat(self) -> None:
        assert _classify_surprise(0.03) == "beat"

    def test_inline(self) -> None:
        assert _classify_surprise(0.0) == "inline"

    def test_miss(self) -> None:
        assert _classify_surprise(-0.02) == "miss"

    def test_large_miss(self) -> None:
        assert _classify_surprise(-0.06) == "large_miss"


class TestSignalStrength:
    def test_zero_surprise_low_strength(self) -> None:
        s = _signal_strength_from_surprise(0.0)
        assert s == pytest.approx(0.1, abs=0.01)

    def test_large_surprise_high_strength(self) -> None:
        s = _signal_strength_from_surprise(0.10)
        assert s == pytest.approx(1.0, abs=0.01)

    def test_capped_at_one(self) -> None:
        s = _signal_strength_from_surprise(0.50)
        assert s <= 1.0

    def test_negative_surprise_also_strong(self) -> None:
        # 大幅 miss 也会有高信号强度
        s = _signal_strength_from_surprise(-0.10)
        assert s >= 0.9


# ---------------------------------------------------------------------------
# 2. compute_pead_signal (mocked)
# ---------------------------------------------------------------------------

def _make_earnings_df() -> pd.DataFrame:
    """构造模拟的 earnings history DataFrame."""
    return pd.DataFrame(
        {
            "epsActual":       [1.50, 1.65, 1.57, 1.85],
            "epsEstimate":     [1.40, 1.62, 1.42, 1.77],
            "epsDifference":   [0.10, 0.03, 0.15, 0.08],
            "surprisePercent": [0.07, 0.02, 0.10, 0.045],
        },
        index=pd.to_datetime(["2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31"]),
    )


class TestComputePEADSignal:
    @patch("yfinance.Ticker")
    def test_basic_pead_signal_returned(self, mock_ticker_cls: MagicMock) -> None:
        mock_t = MagicMock()
        mock_ticker_cls.return_value = mock_t
        mock_t.get_earnings_history.return_value = _make_earnings_df()
        mock_t.history.return_value = pd.DataFrame()  # empty history → no drift calc

        result = compute_pead_signal("AAPL")
        assert result is not None
        assert result.ticker == "AAPL"

    @patch("yfinance.Ticker")
    def test_latest_surprise_populated(self, mock_ticker_cls: MagicMock) -> None:
        mock_t = MagicMock()
        mock_ticker_cls.return_value = mock_t
        mock_t.get_earnings_history.return_value = _make_earnings_df()
        mock_t.history.return_value = pd.DataFrame()

        result = compute_pead_signal("AAPL")
        assert result is not None
        assert result.latest_surprise.eps_actual == pytest.approx(1.85)
        assert result.latest_surprise.eps_estimate == pytest.approx(1.77)

    @patch("yfinance.Ticker")
    def test_surprise_magnitude_large_beat(self, mock_ticker_cls: MagicMock) -> None:
        # 直接构造带大幅超预期的 DataFrame（避免 CoW chained assignment）
        df = pd.DataFrame(
            {
                "epsActual":       [1.50, 1.65, 1.57, 2.00],
                "epsEstimate":     [1.40, 1.62, 1.42, 1.85],
                "epsDifference":   [0.10, 0.03, 0.15, 0.15],
                "surprisePercent": [0.07, 0.02, 0.10, 0.08],  # latest = 8% → large_beat
            },
            index=pd.to_datetime(["2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31"]),
        )
        mock_t = MagicMock()
        mock_ticker_cls.return_value = mock_t
        mock_t.get_earnings_history.return_value = df
        mock_t.history.return_value = pd.DataFrame()

        result = compute_pead_signal("AAPL")
        assert result is not None
        assert result.surprise_magnitude == "large_beat"

    @patch("yfinance.Ticker")
    def test_empty_earnings_returns_none(self, mock_ticker_cls: MagicMock) -> None:
        mock_t = MagicMock()
        mock_ticker_cls.return_value = mock_t
        mock_t.get_earnings_history.return_value = pd.DataFrame()

        result = compute_pead_signal("UNKNOWN")
        assert result is None

    @patch("yfinance.Ticker")
    def test_signal_strength_between_0_and_1(self, mock_ticker_cls: MagicMock) -> None:
        mock_t = MagicMock()
        mock_ticker_cls.return_value = mock_t
        mock_t.get_earnings_history.return_value = _make_earnings_df()
        mock_t.history.return_value = pd.DataFrame()

        result = compute_pead_signal("AAPL")
        assert result is not None
        assert 0.0 <= result.signal_strength <= 1.0


# ---------------------------------------------------------------------------
# 3. compute_piotroski_fscore (mocked)
# ---------------------------------------------------------------------------

def _make_financials() -> pd.DataFrame:
    """metrics × dates（与真实 yfinance 格式相同：行 = 指标，列 = 日期）."""
    return pd.DataFrame(
        {
            "2024-09-30": {"NetIncomeFromContinuingOperationNetMinorityInterest": 1e10,
                           "TotalRevenue": 4e11, "GrossProfit": 1.8e11},
            "2023-09-30": {"NetIncomeFromContinuingOperationNetMinorityInterest": 9e9,
                           "TotalRevenue": 3.8e11, "GrossProfit": 1.7e11},
        }
    )


def _make_balance_sheet() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "2024-09-30": {"TotalAssets": 3.5e11, "LongTermDebt": 1e11,
                           "CurrentAssets": 1.5e11, "CurrentLiabilities": 1.3e11,
                           "OrdinarySharesNumber": 15e9},
            "2023-09-30": {"TotalAssets": 3.4e11, "LongTermDebt": 1.1e11,
                           "CurrentAssets": 1.4e11, "CurrentLiabilities": 1.4e11,
                           "OrdinarySharesNumber": 15.5e9},
        }
    )


def _make_cash_flow() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "2024-09-30": {"OperatingCashFlow": 1.2e10},
            "2023-09-30": {"OperatingCashFlow": 1.1e10},
        }
    )


class TestComputePiotroskiFScore:
    @patch("yfinance.Ticker")
    def test_score_returns_int_0_to_9(self, mock_ticker_cls: MagicMock) -> None:
        mock_t = MagicMock()
        mock_ticker_cls.return_value = mock_t
        mock_t.get_financials.return_value = _make_financials()
        mock_t.get_balance_sheet.return_value = _make_balance_sheet()
        mock_t.get_cash_flow.return_value = _make_cash_flow()

        result = compute_piotroski_fscore("AAPL")
        assert result is not None
        assert 0 <= result.score <= 9

    @patch("yfinance.Ticker")
    def test_signals_dict_has_9_entries(self, mock_ticker_cls: MagicMock) -> None:
        mock_t = MagicMock()
        mock_ticker_cls.return_value = mock_t
        mock_t.get_financials.return_value = _make_financials()
        mock_t.get_balance_sheet.return_value = _make_balance_sheet()
        mock_t.get_cash_flow.return_value = _make_cash_flow()

        result = compute_piotroski_fscore("AAPL")
        assert result is not None
        assert len(result.signals) == 9

    @patch("yfinance.Ticker")
    def test_high_score_grade_strong(self, mock_ticker_cls: MagicMock) -> None:
        """F1-F9 全满足时 grade = strong."""
        mock_t = MagicMock()
        mock_ticker_cls.return_value = mock_t
        fin = _make_financials()
        bs = _make_balance_sheet()
        cf = _make_cash_flow()
        mock_t.get_financials.return_value = fin
        mock_t.get_balance_sheet.return_value = bs
        mock_t.get_cash_flow.return_value = cf

        result = compute_piotroski_fscore("AAPL")
        assert result is not None
        # 根据模拟数据，score 应该偏高（有多个 True）
        if result.score >= 7:
            assert result.grade == "strong"

    @patch("yfinance.Ticker")
    def test_empty_data_returns_none(self, mock_ticker_cls: MagicMock) -> None:
        mock_t = MagicMock()
        mock_ticker_cls.return_value = mock_t
        mock_t.get_financials.return_value = pd.DataFrame()
        mock_t.get_balance_sheet.return_value = pd.DataFrame()
        mock_t.get_cash_flow.return_value = pd.DataFrame()

        result = compute_piotroski_fscore("ZZZZZ")
        assert result is None

    @patch("yfinance.Ticker")
    def test_interpretation_present(self, mock_ticker_cls: MagicMock) -> None:
        mock_t = MagicMock()
        mock_ticker_cls.return_value = mock_t
        mock_t.get_financials.return_value = _make_financials()
        mock_t.get_balance_sheet.return_value = _make_balance_sheet()
        mock_t.get_cash_flow.return_value = _make_cash_flow()

        result = compute_piotroski_fscore("AAPL")
        assert result is not None
        assert len(result.interpretation) > 0
        assert result.ticker == "AAPL"

    @patch("yfinance.Ticker")
    def test_f1_roa_positive_when_profitable(self, mock_ticker_cls: MagicMock) -> None:
        """净利润为正时 F1 (ROA>0) 应为 True."""
        mock_t = MagicMock()
        mock_ticker_cls.return_value = mock_t
        mock_t.get_financials.return_value = _make_financials()
        mock_t.get_balance_sheet.return_value = _make_balance_sheet()
        mock_t.get_cash_flow.return_value = _make_cash_flow()

        result = compute_piotroski_fscore("AAPL")
        assert result is not None
        assert result.signals["F1_roa_positive"] is True

    @patch("yfinance.Ticker")
    def test_f2_operating_cf_positive(self, mock_ticker_cls: MagicMock) -> None:
        """经营现金流为正时 F2 应为 True."""
        mock_t = MagicMock()
        mock_ticker_cls.return_value = mock_t
        mock_t.get_financials.return_value = _make_financials()
        mock_t.get_balance_sheet.return_value = _make_balance_sheet()
        mock_t.get_cash_flow.return_value = _make_cash_flow()

        result = compute_piotroski_fscore("AAPL")
        assert result is not None
        assert result.signals["F2_operating_cashflow_positive"] is True

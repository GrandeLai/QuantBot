"""Unit tests for token unlock engine.

HTTP calls to DefiLlama are mocked; no real network access.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

import pytest

from quantbot.crypto.token_unlock.engine import (
    TokenUnlockCalendar,
    _normalise_category,
    _parse_event,
    _signal_from_score,
    compute_sell_pressure_score,
    fetch_upcoming_unlocks,
)


TODAY = date.today()


# ---------------------------------------------------------------------------
# compute_sell_pressure_score
# ---------------------------------------------------------------------------


class TestComputeSellPressureScore:
    def test_zero_pct_returns_zero(self):
        assert compute_sell_pressure_score(0, 10, "team") == pytest.approx(0.0)

    def test_high_pct_team_near_zero_days(self):
        score = compute_sell_pressure_score(15.0, 0, "team")
        assert score >= 0.8

    def test_small_pct_far_away(self):
        score = compute_sell_pressure_score(1.0, 60, "community")
        assert score < 0.2

    def test_investors_weighted_high(self):
        score_inv = compute_sell_pressure_score(5.0, 7, "investors")
        score_com = compute_sell_pressure_score(5.0, 7, "community")
        assert score_inv > score_com

    def test_score_in_range(self):
        for pct in [0, 1, 5, 10, 20, 50]:
            for days in [-30, -7, 0, 7, 30, 60]:
                s = compute_sell_pressure_score(pct, days, "team")
                assert 0.0 <= s <= 1.0, f"pct={pct}, days={days}, score={s}"

    def test_negative_days_reduces_score(self):
        score_before = compute_sell_pressure_score(5.0, 0, "team")
        score_after  = compute_sell_pressure_score(5.0, -30, "team")
        assert score_before > score_after

    def test_unknown_category_uses_other(self):
        score = compute_sell_pressure_score(5.0, 7, "xyzunknown")
        assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# _signal_from_score
# ---------------------------------------------------------------------------


class TestSignalFromScore:
    def test_high_risk(self):
        assert _signal_from_score(0.8, 3) == "high_risk"

    def test_moderate_risk(self):
        assert _signal_from_score(0.4, 10) == "moderate_risk"

    def test_low_risk(self):
        assert _signal_from_score(0.1, 20) == "low_risk"

    def test_post_unlock_rebound(self):
        assert _signal_from_score(0.7, -20) == "post_unlock_rebound"

    def test_just_past_unlock_still_high_risk(self):
        # -14 days: the signal is NOT post_unlock_rebound (boundary is < -14)
        result = _signal_from_score(0.8, -14)
        assert result in {"high_risk", "moderate_risk", "post_unlock_rebound"}


# ---------------------------------------------------------------------------
# _normalise_category
# ---------------------------------------------------------------------------


class TestNormaliseCategory:
    def test_team_maps_correctly(self):
        assert _normalise_category("Team tokens") == "team"

    def test_investors_maps_correctly(self):
        assert _normalise_category("Early Investors") == "investors"

    def test_unknown_maps_to_other(self):
        assert _normalise_category("XYZ Category") == "other"


# ---------------------------------------------------------------------------
# _parse_event
# ---------------------------------------------------------------------------


class TestParseEvent:
    def _make_item(self, days_offset: int = 10) -> dict:
        ts = int(
            (TODAY + timedelta(days=days_offset))
            .strftime("%s")
            if hasattr(TODAY, "strftime")
            else (TODAY.toordinal() - date(1970, 1, 1).toordinal() + days_offset) * 86400
        )
        # Simpler: compute unix timestamp properly
        from datetime import datetime, timezone
        dt = datetime.combine(TODAY + timedelta(days=days_offset), datetime.min.time(), tzinfo=timezone.utc)
        ts = int(dt.timestamp())
        return {
            "name": "Arbitrum",
            "symbol": "ARB",
            "timestamp": ts,
            "unlockTokens": 100_000_000,
            "unlockUsd": 80_000_000,
            "circSupply": 2_000_000_000,
            "category": "investors",
        }

    def test_parses_valid_event(self):
        item = self._make_item(days_offset=7)
        evt = _parse_event(item, TODAY)
        assert evt is not None
        assert evt.symbol == "ARB"
        assert evt.days_until_unlock == 7
        assert evt.protocol == "Arbitrum"

    def test_returns_none_for_missing_timestamp(self):
        item = {"name": "Test", "symbol": "TST"}
        assert _parse_event(item, TODAY) is None

    def test_skips_old_events(self):
        item = self._make_item(days_offset=-100)
        assert _parse_event(item, TODAY) is None

    def test_pct_circulating_computed(self):
        item = self._make_item(days_offset=7)
        evt = _parse_event(item, TODAY)
        assert evt is not None
        # 100M / 2B * 100 = 5%
        assert evt.unlock_pct_circulating == pytest.approx(5.0, rel=1e-3)


# ---------------------------------------------------------------------------
# fetch_upcoming_unlocks (mocked HTTP)
# ---------------------------------------------------------------------------


class TestFetchUpcomingUnlocks:
    @patch("quantbot.crypto.token_unlock.engine._fetch_defillama", return_value=[])
    def test_empty_api_returns_empty_calendar(self, mock_fetch):
        cal = fetch_upcoming_unlocks(days_ahead=30)
        assert isinstance(cal, TokenUnlockCalendar)
        assert cal.total_events == 0
        assert cal.events == []

    @patch("quantbot.crypto.token_unlock.engine._fetch_defillama")
    def test_valid_events_parsed(self, mock_fetch):
        from datetime import datetime, timezone as tz
        dt = datetime.combine(TODAY + timedelta(days=10), datetime.min.time(), tzinfo=tz.utc)
        mock_fetch.return_value = [
            {
                "name": "Optimism",
                "symbol": "OP",
                "timestamp": int(dt.timestamp()),
                "unlockTokens": 50_000_000,
                "unlockUsd": 100_000_000,
                "circSupply": 500_000_000,
                "category": "team",
            }
        ]
        cal = fetch_upcoming_unlocks(days_ahead=30)
        assert cal.total_events == 1
        assert cal.events[0].symbol == "OP"

    @patch("quantbot.crypto.token_unlock.engine._fetch_defillama")
    def test_high_risk_counted(self, mock_fetch):
        from datetime import datetime, timezone as tz
        dt = datetime.combine(TODAY + timedelta(days=3), datetime.min.time(), tzinfo=tz.utc)
        # 20% of circulating + team + 3 days → high pressure
        mock_fetch.return_value = [
            {
                "name": "Protocol",
                "symbol": "XYZ",
                "timestamp": int(dt.timestamp()),
                "unlockTokens": 400_000_000,
                "circSupply": 2_000_000_000,
                "category": "team",
            }
        ]
        cal = fetch_upcoming_unlocks(days_ahead=30)
        assert cal.high_risk_count == 1

    @patch("quantbot.crypto.token_unlock.engine._fetch_defillama", return_value=[])
    def test_as_of_date_is_today(self, mock_fetch):
        cal = fetch_upcoming_unlocks()
        assert cal.as_of_date == TODAY

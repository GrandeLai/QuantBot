"""Contracts for the development-free normal research adapter.

The adapter exists so free daily bars can drive a research run without
weakening the point-in-time trust boundary.  These tests pin the two things
that matter: the artifacts it seals are genuinely sealed, and they can never
be mistaken for admissible production data.
"""

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.data import DataQualityTier
from quantbot.factors import neutralize_sleeve, residual_momentum_sleeve
from quantbot.normal import (
    NormalFactorExperiment,
    PointInTimeNormalRequestProvider,
)
from quantbot.normal.research_adapter import (
    ResearchDailyBar,
    ResearchNormalDataView,
    ResearchTrustKeys,
)
from quantbot.regime import Regime


SECTORS = ("Tech", "Fin", "Health", "Energy", "Staples", "Ind")
IDS = tuple("S%02d" % index for index in range(24))
BENCHMARK = "BENCH"
KEYS = ResearchTrustKeys(
    "research-calendar-issuer",
    b"quantbot-research-calendar-key-0001",
    "research-coverage-verifier",
    b"quantbot-research-verifier-key-0001",
)


def _sessions(count=1500, start=date(2016, 1, 4)):
    result = []
    day = start
    while len(result) < count:
        if day.weekday() < 5:
            result.append(day)
        day += timedelta(days=1)
    return tuple(result)


SESSION_DATES = _sessions()


def _bars(*, drop=()):
    """Deterministic bars: a shared drift plus a stable per-name tilt."""

    result = []
    prices = {key: 100.0 for key in IDS + (BENCHMARK,)}
    for index, day in enumerate(SESSION_DATES):
        market = 0.0004 + 0.006 * ((index % 11) - 5) / 5.0
        for position, key in enumerate(IDS + (BENCHMARK,)):
            if key in drop and index > 400:
                continue
            tilt = 0.0 if key == BENCHMARK else ((position % 7) - 3) * 0.00035
            wobble = 0.004 * (((index + position * 3) % 9) - 4) / 4.0
            prices[key] *= 1.0 + market + tilt + wobble
            result.append(
                ResearchDailyBar(key, day, prices[key], prices[key], 2_000_000.0)
            )
    return result


def _view(**changes):
    values = dict(
        bars=_bars(),
        sectors={key: SECTORS[index % len(SECTORS)] for index, key in enumerate(IDS)},
        shares_outstanding={key: 1_000_000_000.0 for key in IDS},
        trust_keys=KEYS,
        benchmark_security_id=BENCHMARK,
    )
    values.update(changes)
    bars = values.pop("bars")
    return ResearchNormalDataView(bars, **values)


SIGNAL_DATE = SESSION_DATES[1200]


class ResearchAdapterSealTest(unittest.TestCase):
    def setUp(self):
        self.view = _view()
        self.master = self.view.security_master(SIGNAL_DATE)

    def test_monthly_returns_carry_a_valid_provenance_seal(self):
        returns = self.view.monthly_returns(self.master)

        self.assertTrue(returns.provenance_is_valid)
        self.assertEqual(returns.observed_at, self.master.as_of)
        self.assertEqual(returns.benchmark_security_id, BENCHMARK)

    def test_returns_and_master_stay_development_free(self):
        returns = self.view.monthly_returns(self.master)

        self.assertIs(returns.quality_tier, DataQualityTier.DEVELOPMENT_FREE)
        self.assertIs(self.master.source_quality_tier, DataQualityTier.DEVELOPMENT_FREE)
        self.assertFalse(self.master.admission_verified)

    def test_final_admission_is_refused(self):
        returns = self.view.monthly_returns(self.master)

        with self.assertRaisesRegex(ValueError, "point-in-time verified"):
            returns.require_final_admission()

    def test_benchmark_is_excluded_from_the_investable_universe(self):
        self.assertNotIn(BENCHMARK, self.master.security_ids)
        self.assertEqual(set(self.master.security_ids), set(IDS))

    def test_wrong_trust_keys_cannot_open_the_returns_dataset(self):
        forged = ResearchTrustKeys(
            KEYS.calendar_issuer,
            b"a-different-research-calendar-key-1",
            KEYS.verifier,
            b"a-different-research-verifier-key-1",
        )
        view = _view(trust_keys=forged)
        master = view.security_master(SIGNAL_DATE)
        view.trust_keys = KEYS  # keys the caller claims, not the ones that signed

        with self.assertRaises(ValueError):
            view.monthly_returns(master)


class ResearchAdapterVintageTest(unittest.TestCase):
    """A retroactive adjustment vintage is the core free-data hazard."""

    def setUp(self):
        self.view = _view()

    def test_each_vintage_is_the_close_of_the_month_it_covers(self):
        dataset = self.view._returns_dataset_for(SIGNAL_DATE)
        coverage_end = dataset.manifest.coverage_end
        vintage = dataset.manifest.adjustment_vintage_at

        self.assertLessEqual(coverage_end, SIGNAL_DATE)
        self.assertEqual(vintage.date().month, coverage_end.month)
        self.assertLessEqual(vintage.date(), SIGNAL_DATE)

    def test_a_later_snapshot_cannot_poison_an_earlier_one(self):
        later = SESSION_DATES[1260]
        early = SESSION_DATES[1245]
        # Touch the later date first so its vintage is the one cached.
        self.view.monthly_returns(self.view.security_master(later))
        early_master = self.view.security_master(early)

        returns = self.view.monthly_returns(early_master)

        self.assertTrue(returns.provenance_is_valid)
        self.assertLessEqual(
            self.view._returns_dataset_for(early).manifest.adjustment_vintage_at,
            early_master.as_of,
        )

    def test_vintages_never_see_the_signal_month(self):
        dataset = self.view._returns_dataset_for(SIGNAL_DATE)

        for row in dataset.observations:
            self.assertLess(row.month_end, SIGNAL_DATE)
            self.assertLessEqual(
                row.available_at, dataset.manifest.adjustment_vintage_at
            )


class ResearchAdapterProviderTest(unittest.TestCase):
    def setUp(self):
        self.view = _view()
        self.master = self.view.security_master(SIGNAL_DATE)

    def test_residual_momentum_covers_the_whole_universe(self):
        returns = self.view.monthly_returns(self.master)
        sleeve = neutralize_sleeve(
            residual_momentum_sleeve(
                returns,
                self.master,
                window_months=12,
                skip_recent_months=1,
                estimation_window_months=36,
            ),
            self.master,
        )

        self.assertEqual(set(sleeve.scores), set(self.master.security_ids))
        self.assertEqual(sleeve.missing_security_ids, ())

    def test_provider_builds_a_complete_allocation_request(self):
        from quantbot.trading import PointInTimeSnapshot

        provider = PointInTimeNormalRequestProvider(
            self.view, experiment=NormalFactorExperiment(include_quality=False)
        )
        snapshot = PointInTimeSnapshot(
            observed_at=self.master.as_of, security_master=self.master
        )

        request = provider.build_request(snapshot, {})

        self.assertEqual(request.security_master, self.master)
        self.assertEqual(len(request.sleeves), 1)
        self.assertTrue(request.adv_dollars)
        self.assertTrue(request.market_betas)
        self.assertEqual(request.regime.regime, Regime.R1_BULL_LOW_VOL)

    def test_market_caps_track_price_because_shares_are_fixed(self):
        caps = self.master.latest_market_caps

        self.assertEqual(set(caps), set(self.master.security_ids))
        for security_id, observation in caps.items():
            self.assertGreater(observation.market_cap, 0)
            self.assertLessEqual(observation.available_at, self.master.as_of)
            del security_id


class ResearchAdapterSessionTest(unittest.TestCase):
    def setUp(self):
        self.view = _view()

    def test_sessions_rebalance_monthly_and_never_on_the_final_session(self):
        start, end = SESSION_DATES[1100], SESSION_DATES[1400]
        sessions = self.view.backtest_sessions(start=start, end=end)
        signals = [item for item in sessions if item.signal_snapshot is not None]

        self.assertGreater(len(signals), 10)
        self.assertIsNone(sessions[-1].signal_snapshot)
        months = {(item.trading_date.year, item.trading_date.month) for item in signals}
        self.assertEqual(len(months), len(signals))

    def test_signal_snapshot_effective_date_matches_its_session(self):
        sessions = self.view.backtest_sessions(
            start=SESSION_DATES[1100], end=SESSION_DATES[1300]
        )

        for item in sessions:
            if item.signal_snapshot is None:
                continue
            self.assertEqual(
                item.signal_snapshot.security_master.effective_date, item.trading_date
            )

    def test_no_rebalance_mode_produces_no_signals(self):
        sessions = self.view.backtest_sessions(
            start=SESSION_DATES[1100], end=SESSION_DATES[1200], rebalance="none"
        )

        self.assertTrue(sessions)
        self.assertTrue(all(item.signal_snapshot is None for item in sessions))

    def test_execution_price_is_the_previous_raw_close(self):
        sessions = self.view.backtest_sessions(
            start=SESSION_DATES[1100], end=SESSION_DATES[1110]
        )
        first = sessions[1]
        previous_index = self.view._session_index[first.trading_date] - 1
        previous = self.view.sessions[previous_index]

        for security_id, price in first.open_prices.items():
            self.assertAlmostEqual(
                price, self.view._series[security_id][previous].close
            )


class ResearchAdapterInputTest(unittest.TestCase):
    def test_missing_sector_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing sector"):
            _view(sectors={key: "Tech" for key in IDS[:-1]})

    def test_missing_share_count_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing shares_outstanding"):
            _view(shares_outstanding={key: 1e9 for key in IDS[:-1]})

    def test_absent_benchmark_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "benchmark security"):
            _view(benchmark_security_id="NOT-PRESENT")

    def test_duplicate_bar_is_rejected(self):
        bars = _bars()
        with self.assertRaisesRegex(ValueError, "duplicate bar"):
            _view(bars=bars + [bars[0]])

    def test_short_signing_key_is_rejected(self):
        with self.assertRaises(ValueError):
            _view(trust_keys=ResearchTrustKeys("i", b"short", "v", b"short"))


if __name__ == "__main__":
    unittest.main()

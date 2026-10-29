import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.normal import (
    NormalFactorExperiment,
    NormalPortfolioConfig,
    PointInTimeNormalRequestProvider,
    run_normal_h1_experiment,
)
from quantbot.normal.experiment_runner import _positive_window_fraction


class _Port:
    def monthly_returns(self, security_master):
        raise AssertionError("not reached in identity validation")

    def asset_return_history(self, security_master, *, lookback_sessions):
        return {}

    def sleeve_return_history(self, security_master, sleeves, *, lookback_months):
        return {}

    def market_betas(self, security_master, *, lookback_sessions):
        return {}

    def average_dollar_volume(self, security_master, *, lookback_sessions):
        return {}

    def regime_context(self, security_master):
        raise AssertionError("not reached")


class NormalH1RunnerContractTest(unittest.TestCase):
    def test_runner_rejects_any_experiment_identity_or_top_n_drift_before_data_access(self):
        baseline = PointInTimeNormalRequestProvider(_Port())
        candidate = PointInTimeNormalRequestProvider(
            _Port(), experiment=NormalFactorExperiment(include_quality=True)
        )
        fake_sessions = (object(),)

        with self.assertRaisesRegex(ValueError, "top_n is frozen at 20"):
            run_normal_h1_experiment(
                baseline_provider=baseline,
                candidate_provider=candidate,
                sessions=fake_sessions,
                portfolio_config=NormalPortfolioConfig(top_n=19),
            )
        with self.assertRaisesRegex(ValueError, "baseline must be residual momentum only"):
            run_normal_h1_experiment(
                baseline_provider=candidate,
                candidate_provider=candidate,
                sessions=fake_sessions,
            )
        with self.assertRaisesRegex(ValueError, "candidate must add only"):
            run_normal_h1_experiment(
                baseline_provider=baseline,
                candidate_provider=baseline,
                sessions=fake_sessions,
            )

        shared = _Port()
        shared_baseline = PointInTimeNormalRequestProvider(shared)
        different_view = PointInTimeNormalRequestProvider(
            _Port(),
            experiment=NormalFactorExperiment(include_quality=True),
        )
        with self.assertRaisesRegex(ValueError, "share one point-in-time"):
            run_normal_h1_experiment(
                baseline_provider=shared_baseline,
                candidate_provider=different_view,
                sessions=fake_sessions,
            )

        short_history_baseline = PointInTimeNormalRequestProvider(
            shared,
            asset_lookback_sessions=12,
        )
        short_history_candidate = PointInTimeNormalRequestProvider(
            shared,
            experiment=NormalFactorExperiment(include_quality=True),
            asset_lookback_sessions=12,
        )
        with self.assertRaisesRegex(ValueError, "frozen at 252 sessions"):
            run_normal_h1_experiment(
                baseline_provider=short_history_baseline,
                candidate_provider=short_history_candidate,
                sessions=fake_sessions,
            )

    def test_positive_window_fraction_uses_every_complete_rolling_window(self):
        self.assertAlmostEqual(
            _positive_window_fraction((1.0, -2.0, 1.0, 1.0, 1.0), 3),
            1.0 / 3.0,
        )


if __name__ == "__main__":
    unittest.main()

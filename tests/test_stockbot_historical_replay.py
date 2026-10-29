import io
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.audit import CompletionAudit, load_evidence_file
from quantbot.research_cli import main
from quantbot.historical_replay import build_historical_replay
from quantbot.live import LiveEquitySnapshotJournal, LiveTradingJournal, build_live_trading_evidence
from quantbot.oos_backtest import DailyReturnPoint, MomentumOOSBacktestConfig, run_momentum_oos_backtest
from quantbot.paper import PaperAccountSnapshotJournal, PaperTradingAcceptance, PaperTradingJournal


class StockBotHistoricalReplayTest(unittest.TestCase):
    def test_historical_replay_rolls_daily_returns_into_paper_and_live_logs(self):
        points = _daily_points(date(2024, 1, 2), date(2024, 1, 26), portfolio_return=0.001)

        replay = build_historical_replay(points, initial_equity=100_000.0)
        paper_decision = PaperTradingAcceptance().evaluate(replay.paper_runs)
        live_evidence = build_live_trading_evidence(
            replay.live_sessions,
            min_sessions=10,
            max_drawdown=0.15,
            enforce_policy=False,
        )

        self.assertEqual(len(replay.paper_runs), 4)
        self.assertEqual(len(replay.live_sessions), len(points))
        self.assertTrue(paper_decision.passed)
        self.assertTrue(live_evidence["live_trading_soak"].passed)
        for run in replay.paper_runs:
            self.assertAlmostEqual(run.paper_return, run.backtest_return)

    def test_oos_backtest_exposes_dated_validation_daily_returns_for_replay(self):
        config = MomentumOOSBacktestConfig(
            symbols=("AAA", "BBB", "CCC"),
            benchmark="SPY",
            lookback_days=20,
            top_n=1,
        )

        result = run_momentum_oos_backtest(_synthetic_oos_prices(), config)

        self.assertEqual(len(result.validation_daily_returns), result.validation_observations)
        self.assertGreater(result.validation_observations, 500)
        self.assertGreaterEqual(result.validation_daily_returns[0].trading_date, config.validation_start)

    def test_cli_historical_replay_writes_separate_logs_and_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            evidence_file = root / "historical_evidence.jsonl"
            paper_snapshots = root / "paper_snapshots.jsonl"
            paper_runs = root / "paper_runs.jsonl"
            live_snapshots = root / "live_snapshots.jsonl"
            live_runs = root / "live_runs.jsonl"
            stdout = io.StringIO()

            exit_code = main(
                [
                    "historical-replay",
                    "--evidence-file",
                    str(evidence_file),
                    "--paper-snapshot-log",
                    str(paper_snapshots),
                    "--paper-runs-log",
                    str(paper_runs),
                    "--live-snapshot-log",
                    str(live_snapshots),
                    "--live-runs-log",
                    str(live_runs),
                    "--symbols",
                    "AAA,BBB,CCC",
                    "--benchmark",
                    "SPY",
                    "--lookback-days",
                    "20",
                    "--top-n",
                    "1",
                ],
                stdout=stdout,
                oos_price_loader=lambda symbols, benchmark, start, end: _synthetic_oos_prices(),
            )
            payload = json.loads(stdout.getvalue())
            evidence = load_evidence_file(evidence_file)
            paper_snapshot_count = len(PaperAccountSnapshotJournal(paper_snapshots).read_all())
            paper_run_count = len(PaperTradingJournal(paper_runs).read_all())
            live_snapshot_count = len(LiveEquitySnapshotJournal(live_snapshots).read_all())
            live_run_count = len(LiveTradingJournal(live_runs).read_all())

        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["historical_replay"])
        self.assertFalse(payload["counts_for_real_acceptance"])
        self.assertGreaterEqual(payload["paper"]["runs"], 4)
        self.assertGreaterEqual(payload["live"]["sessions"], 20)
        self.assertTrue(payload["paper"]["status"]["passed"])
        self.assertTrue(payload["live"]["status"]["passed"])
        self.assertGreater(paper_snapshot_count, 0)
        self.assertGreater(paper_run_count, 0)
        self.assertGreater(live_snapshot_count, 0)
        self.assertGreater(live_run_count, 0)
        self.assertIn("historical_replay_only", evidence["paper_trading_4_weeks"].detail)
        self.assertEqual(evidence["paper_trading_4_weeks"].run_type, "historical_replay")
        self.assertEqual(evidence["live_trading_soak"].run_type, "historical_replay")

    def test_replay_journals_cannot_be_laundered_by_paper_or_live_evidence_commands(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            replay_evidence = root / "replay_evidence.jsonl"
            paper_runs = root / "paper_runs.jsonl"
            live_runs = root / "live_runs.jsonl"
            main(
                [
                    "historical-replay",
                    "--evidence-file",
                    str(replay_evidence),
                    "--paper-snapshot-log",
                    str(root / "paper_snapshots.jsonl"),
                    "--paper-runs-log",
                    str(paper_runs),
                    "--live-snapshot-log",
                    str(root / "live_snapshots.jsonl"),
                    "--live-runs-log",
                    str(live_runs),
                    "--symbols",
                    "AAA,BBB,CCC",
                    "--benchmark",
                    "SPY",
                    "--lookback-days",
                    "20",
                    "--top-n",
                    "1",
                ],
                stdout=io.StringIO(),
                oos_price_loader=lambda **_kwargs: _synthetic_oos_prices(),
            )
            washed_evidence = root / "washed_evidence.jsonl"
            main(
                ["paper-evidence", "--log", str(paper_runs), "--evidence-file", str(washed_evidence)],
                stdout=io.StringIO(),
            )
            main(
                [
                    "live-soak-evidence",
                    "--log",
                    str(live_runs),
                    "--evidence-file",
                    str(washed_evidence),
                    "--min-sessions",
                    "20",
                ],
                stdout=io.StringIO(),
            )
            evidence = load_evidence_file(washed_evidence)

        self.assertEqual(evidence["paper_trading_4_weeks"].run_type, "historical_replay")
        self.assertEqual(evidence["live_trading_soak"].run_type, "historical_replay")
        report = CompletionAudit(["paper_trading_4_weeks", "live_trading_soak"]).evaluate(evidence)
        self.assertFalse(report.complete)
        self.assertEqual(report.failed_ids, ["paper_trading_4_weeks", "live_trading_soak"])

    def test_acceptance_cycle_cannot_launder_replay_journal_provenance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paper_snapshots = root / "paper_snapshots.jsonl"
            paper_runs = root / "paper_runs.jsonl"
            live_snapshots = root / "live_snapshots.jsonl"
            live_runs = root / "live_runs.jsonl"
            main(
                [
                    "historical-replay",
                    "--evidence-file",
                    str(root / "replay_evidence.jsonl"),
                    "--paper-snapshot-log",
                    str(paper_snapshots),
                    "--paper-runs-log",
                    str(paper_runs),
                    "--live-snapshot-log",
                    str(live_snapshots),
                    "--live-runs-log",
                    str(live_runs),
                    "--symbols",
                    "AAA,BBB,CCC",
                    "--benchmark",
                    "SPY",
                    "--lookback-days",
                    "20",
                    "--top-n",
                    "1",
                ],
                stdout=io.StringIO(),
                oos_price_loader=lambda **_kwargs: _synthetic_oos_prices(),
            )
            acceptance_evidence = root / "acceptance_evidence.jsonl"
            main(
                [
                    "acceptance-cycle",
                    "--evidence-file",
                    str(acceptance_evidence),
                    "--paper-snapshot-log",
                    str(paper_snapshots),
                    "--paper-runs-log",
                    str(paper_runs),
                    "--skip-paper-snapshot",
                    "--live-snapshot-log",
                    str(live_snapshots),
                    "--live-runs-log",
                    str(live_runs),
                    "--min-live-sessions",
                    "20",
                ],
                stdout=io.StringIO(),
            )
            evidence = load_evidence_file(acceptance_evidence)

        self.assertEqual(evidence["paper_trading_4_weeks"].run_type, "historical_replay")
        self.assertEqual(evidence["live_trading_soak"].run_type, "historical_replay")


def _daily_points(start: date, end: date, portfolio_return: float):
    points = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            points.append(
                DailyReturnPoint(
                    trading_date=current,
                    portfolio_return=portfolio_return,
                    benchmark_return=portfolio_return / 2,
                )
            )
        current += timedelta(days=1)
    return points


def _synthetic_oos_prices():
    rates = {
        "AAA": (0.0012, 0.0004),
        "BBB": (0.0002, 0.0002),
        "CCC": (-0.0001, 0.0002),
        "SPY": (0.00035, 0.00015),
    }
    prices = {ticker: {} for ticker in rates}
    levels = {ticker: 100.0 for ticker in rates}
    current = date(2014, 1, 1)
    end = date(2024, 12, 31)
    index = 0
    while current <= end:
        if current.weekday() < 5:
            for ticker, (base, amplitude) in rates.items():
                levels[ticker] *= 1 + base + amplitude * ((index % 5) - 2)
                prices[ticker][current] = levels[ticker]
            index += 1
        current += timedelta(days=1)
    return prices


if __name__ == "__main__":
    unittest.main()

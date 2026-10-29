import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.audit import RequirementEvidence
from quantbot.research_cli import main
from quantbot.integrations.http import ProviderRateLimitPolicy, RateLimitError, RetryingJsonTransport
from quantbot.paper import (
    PaperAccountSnapshotJournal,
    PaperTradingJournal,
    build_paper_trading_evidence,
    paper_period_for_timestamp,
    rollup_paper_snapshots,
)


class StockBotPaperEvidenceRateLimitTest(unittest.TestCase):
    def test_retrying_transport_honors_retry_after_for_rate_limits(self):
        calls = []
        sleeps = []

        def request_json(_url, _headers, _params):
            calls.append("call")
            if len(calls) == 1:
                raise RateLimitError("too many requests", retry_after=2.5)
            return {"ok": True}

        transport = RetryingJsonTransport(
            request_json=request_json,
            sleep=sleeps.append,
            max_attempts=2,
            backoff_seconds=0.1,
        )

        self.assertEqual(transport.get_json("https://example.com"), {"ok": True})
        self.assertEqual(sleeps, [2.5])

    def test_provider_rate_limit_policy_parses_retry_after_and_reset_headers(self):
        policy = ProviderRateLimitPolicy(provider="alpaca")

        self.assertEqual(policy.retry_after({"Retry-After": "3"}, now=100), 3.0)
        self.assertEqual(policy.retry_after({"x-ratelimit-reset": "105"}, now=100), 5.0)
        self.assertEqual(ProviderRateLimitPolicy(provider="polygon").retry_after({"x-rate-limit-reset": "112"}, now=100), 12.0)

    def test_provider_rate_limit_policy_raises_rate_limit_error_when_remaining_is_zero(self):
        policy = ProviderRateLimitPolicy(provider="alpaca")

        with self.assertRaises(RateLimitError) as context:
            policy.raise_if_limited({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "105"}, now=100)

        self.assertEqual(context.exception.retry_after, 5.0)

    def test_paper_trading_journal_persists_and_reads_runs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "paper_runs.jsonl"
            journal = PaperTradingJournal(path)
            journal.append(period="week1", errors=0, paper_return=0.01, backtest_return=0.011)
            journal.append(period="week2", errors=1, paper_return=0.00, backtest_return=0.010)

            runs = journal.read_all()

        self.assertEqual([run.period for run in runs], ["week1", "week2"])
        self.assertEqual(runs[1].errors, 1)

    def test_build_paper_evidence_returns_requirement_evidence_for_duration_and_deviation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "paper_runs.jsonl"
            journal = PaperTradingJournal(path)
            for week in range(1, 5):
                journal.append(period="week%d" % week, errors=0, paper_return=0.01, backtest_return=0.01)

            evidence = build_paper_trading_evidence(journal.read_all())

        self.assertIsInstance(evidence["paper_trading_4_weeks"], RequirementEvidence)
        self.assertTrue(evidence["paper_trading_4_weeks"].passed)
        self.assertTrue(evidence["paper_vs_backtest_deviation"].passed)

    def test_cli_paper_acceptance_can_read_runs_from_log_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "paper_runs.jsonl"
            journal = PaperTradingJournal(path)
            for week in range(1, 5):
                journal.append(period="week%d" % week, errors=0, paper_return=0.01, backtest_return=0.01)
            stdout = io.StringIO()

            exit_code = main(["paper-acceptance", "--runs-log", str(path)], stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["passed"])
        self.assertEqual(payload["weeks"], 4)

    def test_paper_account_snapshots_roll_up_to_weekly_runs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "paper_snapshots.jsonl"
            journal = PaperAccountSnapshotJournal(path)
            journal.append(timestamp="2026-07-06T21:00:00Z", equity=100000.0, errors=0)
            journal.append(timestamp="2026-07-10T21:00:00Z", equity=101000.0, errors=0)
            journal.append(timestamp="2026-07-13T21:00:00Z", equity=101000.0, errors=1)
            journal.append(timestamp="2026-07-17T21:00:00Z", equity=100495.0, errors=0)

            rollup = rollup_paper_snapshots(
                journal.read_all(),
                backtest_returns={"2026-W28": 0.008, "2026-W29": -0.004},
            )

        self.assertEqual(paper_period_for_timestamp("2026-07-06T21:00:00Z"), "2026-W28")
        self.assertEqual([run.period for run in rollup.runs], ["2026-W28", "2026-W29"])
        self.assertAlmostEqual(rollup.runs[0].paper_return, 0.01)
        self.assertEqual(rollup.runs[1].errors, 1)
        self.assertEqual(rollup.missing_backtest_periods, [])
        self.assertEqual(rollup.skipped_periods, [])

    def test_paper_snapshot_rollup_reports_missing_backtest_periods_without_fabricating_runs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "paper_snapshots.jsonl"
            journal = PaperAccountSnapshotJournal(path)
            journal.append(timestamp="2026-07-06T21:00:00Z", equity=100000.0)
            journal.append(timestamp="2026-07-10T21:00:00Z", equity=101000.0)

            rollup = rollup_paper_snapshots(journal.read_all(), backtest_returns={})

        self.assertEqual(rollup.runs, [])
        self.assertEqual(rollup.missing_backtest_periods, ["2026-W28"])


if __name__ == "__main__":
    unittest.main()

import importlib
import inspect
import io
import json
import sys
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.audit import CompletionAudit, RequirementEvidence, load_evidence_file
from quantbot.research_cli import main
from quantbot.evidence import EvidenceRecorder
from quantbot.live import (
    LiveTradingJournal,
    LiveTradingRun,
    build_live_trading_evidence,
)
from quantbot.paper import (
    PaperTradingJournal,
    PaperTradingRun,
    build_paper_trading_evidence,
)


class StockBotAcceptancePolicyTest(unittest.TestCase):
    def test_current_policy_centralizes_official_thresholds_and_only_allows_stricter_overrides(self):
        policy_module = _load_policy_module(self)
        current = policy_module.CURRENT_ACCEPTANCE_POLICY

        self.assertEqual(policy_module.ACCEPTANCE_POLICY_VERSION, "quantbot.acceptance.v1")
        self.assertEqual(current.policy_version, "quantbot.acceptance.v1")
        self.assertEqual(current.min_paper_weeks, 4)
        self.assertEqual(current.max_paper_performance_deviation, 0.30)
        self.assertEqual(current.min_live_sessions, 20)
        self.assertEqual(current.max_live_drawdown, 0.15)

        loosened = current.effective(
            min_paper_weeks=1,
            max_paper_performance_deviation=0.90,
            min_live_sessions=1,
            max_live_drawdown=0.90,
        )
        self.assertEqual(loosened, current)

        stricter = current.effective(
            min_paper_weeks=5,
            max_paper_performance_deviation=0.20,
            min_live_sessions=21,
            max_live_drawdown=0.10,
        )
        self.assertEqual(stricter.min_paper_weeks, 5)
        self.assertEqual(stricter.max_paper_performance_deviation, 0.20)
        self.assertEqual(stricter.min_live_sessions, 21)
        self.assertEqual(stricter.max_live_drawdown, 0.10)

    def test_evidence_policy_version_is_optional_for_legacy_offline_records(self):
        policy_module = _load_policy_module(self)
        field_names = {field.name for field in fields(RequirementEvidence)}
        record_parameters = inspect.signature(EvidenceRecorder.record).parameters
        self.assertIn("policy_version", field_names)
        self.assertIn("policy_version", record_parameters)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "evidence.jsonl"
            recorder = EvidenceRecorder(path)
            recorder.record("offline_tests", True, "legacy offline")
            recorder.record(
                "paper_trading_4_weeks",
                True,
                "formal paper",
                policy_version=policy_module.ACCEPTANCE_POLICY_VERSION,
            )
            raw_records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            evidence = load_evidence_file(path)

        self.assertNotIn("policy_version", raw_records[0])
        self.assertEqual(
            raw_records[1]["policy_version"],
            policy_module.ACCEPTANCE_POLICY_VERSION,
        )
        self.assertIsNone(evidence["offline_tests"].policy_version)
        self.assertEqual(
            evidence["paper_trading_4_weeks"].policy_version,
            policy_module.ACCEPTANCE_POLICY_VERSION,
        )

    def test_completion_audit_requires_current_policy_for_formal_paper_and_live_only(self):
        policy_module = _load_policy_module(self)
        self.assertIn("policy_version", {field.name for field in fields(RequirementEvidence)})

        def item(requirement_id, run_type, policy_version=None):
            return RequirementEvidence(
                requirement_id,
                True,
                "claimed pass",
                run_type=run_type,
                observed_at="2026-07-10T01:02:03Z",
                dataset_hash="a" * 64,
                config_hash="b" * 64,
                code_hash="c" * 64,
                policy_version=policy_version,
            )

        current = policy_module.ACCEPTANCE_POLICY_VERSION
        accepted = CompletionAudit(
            ["offline_tests", "paper_trading_4_weeks", "live_trading_soak"]
        ).evaluate(
            {
                "offline_tests": RequirementEvidence("offline_tests", True, "legacy offline"),
                "paper_trading_4_weeks": item(
                    "paper_trading_4_weeks", "paper_trading", current
                ),
                "live_trading_soak": item("live_trading_soak", "live_trading", current),
            }
        )
        rejected = CompletionAudit(
            ["paper_trading_4_weeks", "paper_vs_backtest_deviation", "live_trading_soak"]
        ).evaluate(
            {
                "paper_trading_4_weeks": item(
                    "paper_trading_4_weeks", "paper_trading", "quantbot.acceptance.old"
                ),
                "paper_vs_backtest_deviation": item(
                    "paper_vs_backtest_deviation", "paper_trading", None
                ),
                "live_trading_soak": item(
                    "live_trading_soak", "live_trading", "quantbot.acceptance.future"
                ),
            }
        )

        self.assertTrue(accepted.complete)
        self.assertEqual(accepted.passed_ids, [
            "offline_tests",
            "paper_trading_4_weeks",
            "live_trading_soak",
        ])
        self.assertFalse(rejected.complete)
        self.assertEqual(
            rejected.failed_ids,
            ["paper_trading_4_weeks", "paper_vs_backtest_deviation", "live_trading_soak"],
        )

    def test_formal_evidence_cannot_lower_official_duration_minimums(self):
        policy_module = _load_policy_module(self)
        paper = build_paper_trading_evidence(
            [PaperTradingRun("2026-W01", 0, 0.01, 0.01, run_type="paper_trading")],
            min_weeks=1,
            max_performance_deviation=0.90,
        )
        live = build_live_trading_evidence(
            [LiveTradingRun("2026-07-01", 0, 0.01, run_type="live_trading")],
            min_sessions=1,
            max_drawdown=0.90,
        )

        self.assertFalse(paper["paper_trading_4_weeks"].passed)
        self.assertIn("duration", paper["paper_trading_4_weeks"].detail)
        self.assertEqual(
            paper["paper_trading_4_weeks"].policy_version,
            policy_module.ACCEPTANCE_POLICY_VERSION,
        )
        self.assertFalse(live["live_trading_soak"].passed)
        self.assertIn("duration", live["live_trading_soak"].detail)
        self.assertEqual(
            live["live_trading_soak"].policy_version,
            policy_module.ACCEPTANCE_POLICY_VERSION,
        )

    def test_formal_evidence_honors_stricter_thresholds(self):
        paper_runs = [
            PaperTradingRun(
                "2026-W%02d" % week,
                0,
                0.0125,
                0.01,
                run_type="paper_trading",
            )
            for week in range(1, 5)
        ]
        live_runs = [
            LiveTradingRun(
                "2026-07-%02d" % day,
                0,
                0.12,
                run_type="live_trading",
            )
            for day in range(1, 21)
        ]

        paper = build_paper_trading_evidence(
            paper_runs,
            min_weeks=5,
            max_performance_deviation=0.20,
        )
        live = build_live_trading_evidence(
            live_runs,
            min_sessions=21,
            max_drawdown=0.10,
        )

        self.assertFalse(paper["paper_trading_4_weeks"].passed)
        self.assertFalse(paper["paper_vs_backtest_deviation"].passed)
        self.assertIn("duration", paper["paper_trading_4_weeks"].detail)
        self.assertIn("performance_deviation", paper["paper_vs_backtest_deviation"].detail)
        self.assertFalse(live["live_trading_soak"].passed)
        self.assertIn("duration", live["live_trading_soak"].detail)
        self.assertIn("drawdown", live["live_trading_soak"].detail)

    def test_cli_uses_effective_policy_for_status_evidence_and_config_hash(self):
        policy_module = _load_policy_module(self)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paper_log = root / "paper.jsonl"
            live_log = root / "live.jsonl"
            PaperTradingJournal(paper_log).append("2026-W01", 0, 0.01, 0.01)
            for day in range(1, 21):
                LiveTradingJournal(live_log).append(
                    "2026-07-%02d" % day,
                    0,
                    0.01,
                )

            paper_stdout = io.StringIO()
            paper_exit = main(
                ["paper-status", "--log", str(paper_log), "--min-weeks", "1"],
                stdout=paper_stdout,
            )
            live_stdout = io.StringIO()
            live_exit = main(
                ["live-soak-status", "--log", str(live_log), "--min-sessions", "1"],
                stdout=live_stdout,
            )

            default_file = root / "default.jsonl"
            loosened_file = root / "loosened.jsonl"
            stricter_file = root / "stricter.jsonl"
            default_exit = main(
                [
                    "live-soak-evidence",
                    "--log",
                    str(live_log),
                    "--evidence-file",
                    str(default_file),
                ],
                stdout=io.StringIO(),
            )
            loosened_exit = main(
                [
                    "live-soak-evidence",
                    "--log",
                    str(live_log),
                    "--evidence-file",
                    str(loosened_file),
                    "--min-sessions",
                    "1",
                    "--max-drawdown",
                    "0.90",
                ],
                stdout=io.StringIO(),
            )
            stricter_exit = main(
                [
                    "live-soak-evidence",
                    "--log",
                    str(live_log),
                    "--evidence-file",
                    str(stricter_file),
                    "--min-sessions",
                    "21",
                ],
                stdout=io.StringIO(),
            )
            default = load_evidence_file(default_file)["live_trading_soak"]
            loosened = load_evidence_file(loosened_file)["live_trading_soak"]
            stricter = load_evidence_file(stricter_file)["live_trading_soak"]

        paper_payload = json.loads(paper_stdout.getvalue())
        live_payload = json.loads(live_stdout.getvalue())
        self.assertEqual(paper_exit, 1)
        self.assertEqual(paper_payload["remaining_weeks"], 3)
        self.assertEqual(paper_payload["min_weeks"], 4)
        self.assertEqual(live_exit, 0)
        self.assertEqual(live_payload["min_sessions"], 20)
        self.assertEqual(default_exit, 0)
        self.assertEqual(loosened_exit, 0)
        self.assertEqual(stricter_exit, 1)
        self.assertEqual(default.policy_version, policy_module.ACCEPTANCE_POLICY_VERSION)
        self.assertEqual(loosened.policy_version, policy_module.ACCEPTANCE_POLICY_VERSION)
        self.assertEqual(stricter.policy_version, policy_module.ACCEPTANCE_POLICY_VERSION)
        self.assertEqual(default.config_hash, loosened.config_hash)
        self.assertNotEqual(default.config_hash, stricter.config_hash)


def _load_policy_module(testcase):
    try:
        return importlib.import_module("quantbot.acceptance_policy")
    except ModuleNotFoundError:
        testcase.fail("quantbot.acceptance_policy is required")


if __name__ == "__main__":
    unittest.main()

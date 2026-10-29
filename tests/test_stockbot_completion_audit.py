import io
import json
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.audit import CompletionAudit, RequirementEvidence
from quantbot.acceptance_policy import ACCEPTANCE_POLICY_VERSION
from quantbot.research_cli import main


class StockBotCompletionAuditTest(unittest.TestCase):
    def test_completion_audit_reports_missing_external_evidence(self):
        audit = CompletionAudit.design_default()
        evidence = {
            "offline_tests": RequirementEvidence("offline_tests", passed=True, detail="74 tests"),
            "paper_trading_4_weeks": RequirementEvidence("paper_trading_4_weeks", passed=False, detail="no paper run log"),
        }

        report = audit.evaluate(evidence)

        self.assertFalse(report.complete)
        self.assertIn("paper_trading_4_weeks", report.failed_ids)
        self.assertIn("live_api_smoke", report.missing_ids)

    def test_completion_audit_passes_only_when_every_requirement_has_passing_evidence(self):
        audit = CompletionAudit(
            [
                "offline_tests",
                "live_api_smoke",
            ]
        )

        report = audit.evaluate(
            {
                "offline_tests": RequirementEvidence("offline_tests", True, "ok"),
                "live_api_smoke": RequirementEvidence(
                    "live_api_smoke",
                    True,
                    "ok",
                    run_type="live_api_smoke",
                    observed_at="2026-07-10T01:02:03Z",
                    dataset_hash="a" * 64,
                    config_hash="b" * 64,
                    code_hash="c" * 64,
                ),
            }
        )

        self.assertTrue(report.complete)
        self.assertEqual(report.failed_ids, [])
        self.assertEqual(report.missing_ids, [])

    def test_completion_audit_rejects_paper_or_live_evidence_without_provenance(self):
        audit = CompletionAudit(["offline_tests", "paper_trading_4_weeks", "live_trading_soak"])

        report = audit.evaluate(
            {
                "offline_tests": RequirementEvidence("offline_tests", True, "legacy offline remains valid"),
                "paper_trading_4_weeks": RequirementEvidence("paper_trading_4_weeks", True, "legacy paper"),
                "live_trading_soak": RequirementEvidence("live_trading_soak", True, "legacy live"),
            }
        )

        self.assertEqual(report.passed_ids, ["offline_tests"])
        self.assertEqual(report.failed_ids, ["paper_trading_4_weeks", "live_trading_soak"])

    def test_completion_audit_rejects_oos_backtest_without_historical_provenance(self):
        audit = CompletionAudit(["oos_backtest_acceptance"])

        missing = audit.evaluate(
            {
                "oos_backtest_acceptance": RequirementEvidence(
                    "oos_backtest_acceptance",
                    True,
                    "legacy result without provenance",
                )
            }
        )
        wrong_run_type = audit.evaluate(
            {
                "oos_backtest_acceptance": RequirementEvidence(
                    "oos_backtest_acceptance",
                    True,
                    "paper data mislabeled as OOS",
                    run_type="paper_trading",
                    observed_at="2026-07-10T01:02:03Z",
                    dataset_hash="a" * 64,
                    config_hash="b" * 64,
                    code_hash="c" * 64,
                )
            }
        )

        self.assertEqual(missing.failed_ids, ["oos_backtest_acceptance"])
        self.assertEqual(wrong_run_type.failed_ids, ["oos_backtest_acceptance"])

    def test_completion_audit_rejects_historical_replay_for_live_requirement(self):
        audit = CompletionAudit(["live_trading_soak"])

        report = audit.evaluate(
            {
                "live_trading_soak": RequirementEvidence(
                    "live_trading_soak",
                    True,
                    "twenty historical sessions",
                    run_type="historical_replay",
                    observed_at="2026-07-10T01:02:03Z",
                    dataset_hash="a" * 64,
                    config_hash="b" * 64,
                    code_hash="c" * 64,
                )
            }
        )

        self.assertFalse(report.complete)
        self.assertEqual(report.failed_ids, ["live_trading_soak"])

    def test_completion_audit_rejects_mismatched_or_arbitrary_run_types(self):
        def evidence(requirement_id, run_type):
            return RequirementEvidence(
                requirement_id,
                True,
                "claimed pass",
                run_type=run_type,
                observed_at="2026-07-10T01:02:03Z",
                dataset_hash="a" * 64,
                config_hash="b" * 64,
                code_hash="c" * 64,
            )

        audit = CompletionAudit(
            [
                "paper_trading_4_weeks",
                "live_trading_soak",
                "live_api_smoke",
                "paper_new_typo",
            ]
        )
        report = audit.evaluate(
            {
                "paper_trading_4_weeks": evidence("paper_trading_4_weeks", "live_trading"),
                "live_trading_soak": evidence("live_trading_soak", "arbitrary"),
                "live_api_smoke": evidence("live_api_smoke", "live_trading"),
                "paper_new_typo": evidence("paper_new_typo", "paper_trading"),
            }
        )

        self.assertFalse(report.complete)
        self.assertEqual(
            report.failed_ids,
            [
                "paper_trading_4_weeks",
                "live_trading_soak",
                "live_api_smoke",
                "paper_new_typo",
            ],
        )

    def test_completion_audit_requires_passed_to_be_actual_bool_true(self):
        item = RequirementEvidence(
            "live_api_smoke",
            "false",
            "direct construction bypass",
            run_type="live_api_smoke",
            observed_at="2026-07-10T01:02:03Z",
            dataset_hash="a" * 64,
            config_hash="b" * 64,
            code_hash="c" * 64,
        )

        report = CompletionAudit(["live_api_smoke"]).evaluate({"live_api_smoke": item})

        self.assertFalse(report.complete)
        self.assertEqual(report.failed_ids, ["live_api_smoke"])

    def test_provenance_rejects_naive_observation_time_and_noncanonical_hashes(self):
        def item(observed_at, dataset_hash):
            return RequirementEvidence(
                "normal_strategy_admission",
                True,
                "claimed pass",
                run_type="sealed_backtest",
                observed_at=observed_at,
                dataset_hash=dataset_hash,
                config_hash="b" * 64,
                code_hash="c" * 64,
            )

        naive = CompletionAudit(["normal_strategy_admission"]).evaluate(
            {"normal_strategy_admission": item("2026-07-10T01:02:03", "a" * 64)}
        )
        uppercase = CompletionAudit(["normal_strategy_admission"]).evaluate(
            {
                "normal_strategy_admission": item(
                    "2026-07-10T01:02:03Z",
                    "A" * 64,
                )
            }
        )

        self.assertFalse(naive.complete)
        self.assertFalse(uppercase.complete)

    def test_strategy_admission_shape_alone_cannot_bypass_authentication(self):
        def item(requirement_id, cost_model_hash):
            experiment_id = (
                "normal-h1-quality-diversification"
                if requirement_id == "normal_strategy_admission"
                else "daytrade-h9-rvol-reward-risk"
            )
            return RequirementEvidence(
                requirement_id,
                True,
                "sealed admission",
                run_type="sealed_backtest",
                observed_at="2028-08-01T00:00:00Z",
                dataset_hash="a" * 64,
                config_hash="b" * 64,
                cost_model_hash=cost_model_hash,
                code_hash="d" * 64,
                policy_version=ACCEPTANCE_POLICY_VERSION,
                input_materialization_hash="1" * 64,
                output_hash="2" * 64,
                report_hash="3" * 64,
                cost_suite_hash="4" * 64,
                run_id="sealed-run",
                experiment_id=experiment_id,
            )

        for requirement_id in (
            "normal_strategy_admission",
            "daytrade_strategy_admission",
        ):
            with self.subTest(requirement_id=requirement_id):
                missing = CompletionAudit([requirement_id]).evaluate(
                    {requirement_id: item(requirement_id, None)}
                )
                valid = CompletionAudit([requirement_id]).evaluate(
                    {requirement_id: item(requirement_id, "c" * 64)}
                )

                self.assertFalse(missing.complete)
                self.assertFalse(valid.complete)

    def test_strategy_admission_rejects_legacy_evidence_without_run_report_chain(self):
        legacy = RequirementEvidence(
            "normal_strategy_admission",
            True,
            "legacy claimed pass",
            run_type="sealed_backtest",
            observed_at="2028-08-01T00:00:00Z",
            dataset_hash="a" * 64,
            config_hash="b" * 64,
            cost_model_hash="c" * 64,
            code_hash="d" * 64,
            policy_version=ACCEPTANCE_POLICY_VERSION,
        )

        report = CompletionAudit(["normal_strategy_admission"]).evaluate(
            {"normal_strategy_admission": legacy}
        )

        self.assertFalse(report.complete)

    def test_cli_completion_audit_reads_evidence_json(self):
        stdout = io.StringIO()
        evidence_json = json.dumps(
            {
                "offline_tests": {"passed": True, "detail": "ok"},
                "paper_trading_4_weeks": {"passed": False, "detail": "missing"},
            }
        )

        exit_code = main(["completion-audit", "--evidence-json", evidence_json], stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["complete"])
        self.assertIn("paper_trading_4_weeks", payload["failed_ids"])

    def test_cli_completion_audit_preserves_json_provenance_fields(self):
        stdout = io.StringIO()
        evidence_json = json.dumps(
            {
                "live_api_smoke": {
                    "passed": True,
                    "detail": "smoke ok",
                    "run_type": "live_api_smoke",
                    "observed_at": "2026-07-10T01:02:03Z",
                    "dataset_hash": "a" * 64,
                    "config_hash": "b" * 64,
                    "code_hash": "c" * 64,
                }
            }
        )

        main(["completion-audit", "--evidence-json", evidence_json], stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertIn("live_api_smoke", payload["passed_ids"])

    def test_cli_completion_audit_does_not_treat_string_false_as_passed(self):
        stdout = io.StringIO()
        evidence_json = json.dumps(
            {
                "live_api_smoke": {
                    "passed": "false",
                    "detail": "not a boolean",
                    "run_type": "live_api_smoke",
                    "observed_at": "2026-07-10T01:02:03Z",
                    "dataset_hash": "a" * 64,
                    "config_hash": "b" * 64,
                    "code_hash": "c" * 64,
                }
            }
        )

        main(["completion-audit", "--evidence-json", evidence_json], stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertIn("live_api_smoke", payload["failed_ids"])
        self.assertNotIn("live_api_smoke", payload["passed_ids"])


if __name__ == "__main__":
    unittest.main()

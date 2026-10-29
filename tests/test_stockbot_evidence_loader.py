import io
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.audit import RequirementEvidence, load_evidence_file
from quantbot.research_cli import main
from quantbot.evidence import EvidenceRecorder
from quantbot.evidence_auth import EvidenceAppendConflictError
import quantbot.evidence as evidence_module


class StockBotEvidenceLoaderTest(unittest.TestCase):
    def test_evidence_recorder_supports_atomic_idempotent_materialization(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "evidence.jsonl"
            recorder = EvidenceRecorder(path)
            values = {
                "run_type": "historical_backtest",
                "observed_at": "2026-07-10T01:02:03Z",
                "dataset_hash": "a" * 64,
                "config_hash": "b" * 64,
                "code_hash": "c" * 64,
                "report_hash": "d" * 64,
                "run_id": "development-v1",
                "experiment_id": "experiment-v1",
                "idempotency_fields": (
                    "requirement_id",
                    "run_id",
                    "report_hash",
                ),
            }

            self.assertTrue(recorder.record("experiment", False, "rejected", **values))
            self.assertFalse(recorder.record("experiment", False, "rejected", **values))
            with self.assertRaises(EvidenceAppendConflictError):
                recorder.record("experiment", True, "changed", **values)

            self.assertEqual(
                len(path.read_text(encoding="utf-8").splitlines()),
                1,
            )

    def test_evidence_recorder_writes_and_loader_preserves_provenance(self):
        self.assertTrue(hasattr(evidence_module, "hash_payload"))
        self.assertIn("run_type", inspect.signature(EvidenceRecorder.record).parameters)
        hash_payload = evidence_module.hash_payload
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "evidence.jsonl"
            recorder = EvidenceRecorder(path)
            dataset_hash = hash_payload({"bars": 390, "symbol": "AAPL"})
            config_hash = hash_payload({"lookback_days": 20})

            recorder.record(
                "oos_backtest_acceptance",
                True,
                "net backtest passed",
                run_type="historical_backtest",
                observed_at="2026-07-10T01:02:03Z",
                dataset_hash=dataset_hash,
                config_hash=config_hash,
            )
            raw = json.loads(path.read_text(encoding="utf-8"))
            evidence = load_evidence_file(path)["oos_backtest_acceptance"]

        self.assertEqual(raw["run_type"], "historical_backtest")
        self.assertEqual(raw["observed_at"], "2026-07-10T01:02:03Z")
        self.assertEqual(raw["dataset_hash"], dataset_hash)
        self.assertEqual(raw["config_hash"], config_hash)
        self.assertEqual(len(raw["code_hash"]), 64)
        self.assertEqual(evidence.run_type, "historical_backtest")
        self.assertEqual(evidence.dataset_hash, dataset_hash)

    def test_source_tree_hash_is_deterministic_and_content_sensitive(self):
        self.assertTrue(hasattr(evidence_module, "source_tree_hash"))
        source_tree_hash = evidence_module.source_tree_hash
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "module.py").write_text("VALUE = 1\n", encoding="utf-8")

            first = source_tree_hash(root)
            second = source_tree_hash(root)
            (root / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
            changed = source_tree_hash(root)

        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)

    def test_load_evidence_file_reads_requirement_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "evidence.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps({"requirement_id": "offline_tests", "passed": True, "detail": "81 tests"}),
                        json.dumps({"requirement_id": "live_api_smoke", "passed": False, "detail": "missing credentials"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            evidence = load_evidence_file(path)

        self.assertIsInstance(evidence["offline_tests"], RequirementEvidence)
        self.assertTrue(evidence["offline_tests"].passed)
        self.assertFalse(evidence["live_api_smoke"].passed)
        self.assertIsNone(evidence["offline_tests"].run_type)

    def test_load_evidence_file_does_not_coerce_string_false_to_true(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "evidence.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "requirement_id": "offline_tests",
                        "passed": "false",
                        "detail": "invalid producer",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            evidence = load_evidence_file(path)

        self.assertFalse(evidence["offline_tests"].passed)

    def test_completion_audit_cli_accepts_evidence_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "evidence.jsonl"
            path.write_text(
                json.dumps({"requirement_id": "offline_tests", "passed": True, "detail": "ok"}) + "\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()

            exit_code = main(["completion-audit", "--evidence-file", str(path)], stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertIn("live_api_smoke", payload["missing_ids"])
        self.assertEqual(payload["passed_ids"], ["offline_tests"])

    def test_completion_audit_cli_returns_json_error_for_missing_evidence_file(self):
        stdout = io.StringIO()

        exit_code = main(["completion-audit", "--evidence-file", "/tmp/quantbot-missing-evidence.jsonl"], stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["error"], "evidence_file_not_found")


if __name__ == "__main__":
    unittest.main()

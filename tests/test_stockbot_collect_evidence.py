import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.audit import load_evidence_file, load_evidence_records
from quantbot.research_cli import main
from quantbot.evidence import EvidenceRecorder


class StockBotCollectEvidenceTest(unittest.TestCase):
    def test_evidence_recorder_appends_requirement_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "evidence.jsonl"
            recorder = EvidenceRecorder(path)

            recorder.record("offline_tests", True, "87 tests")
            recorder.record("duckdb_parquet_runtime", False, "duckdb missing")
            evidence = load_evidence_file(path)

        self.assertTrue(evidence["offline_tests"].passed)
        self.assertFalse(evidence["duckdb_parquet_runtime"].passed)

    def test_evidence_recorder_preserves_input_and_output_identities(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "evidence.jsonl"
            recorder = EvidenceRecorder(path)
            recorder.record(
                "experiment",
                True,
                "complete",
                dataset_hash="a" * 64,
                input_materialization_hash="a" * 64,
                output_hash="b" * 64,
                report_hash="c" * 64,
                cost_suite_hash="d" * 64,
                run_id="run-1",
                experiment_id="normal-h1",
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            evidence = load_evidence_file(path)["experiment"]

        self.assertEqual(payload["dataset_hash"], "a" * 64)
        self.assertEqual(payload["input_materialization_hash"], "a" * 64)
        self.assertEqual(payload["output_hash"], "b" * 64)
        self.assertEqual(payload["report_hash"], "c" * 64)
        self.assertEqual(evidence.cost_suite_hash, "d" * 64)
        self.assertEqual(evidence.run_id, "run-1")
        self.assertEqual(evidence.experiment_id, "normal-h1")

    def test_evidence_recorder_keeps_logical_and_materialized_identities_separate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "evidence.jsonl"
            recorder = EvidenceRecorder(path)
            recorder.record(
                "experiment",
                True,
                "valid",
                dataset_hash="a" * 64,
                input_materialization_hash="b" * 64,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["dataset_hash"], "a" * 64)
        self.assertEqual(payload["input_materialization_hash"], "b" * 64)

    def test_all_record_loader_preserves_runs_while_completion_loader_is_latest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "evidence.jsonl"
            recorder = EvidenceRecorder(path)
            recorder.record("experiment", False, "first", run_id="cost-5")
            recorder.record("experiment", True, "second", run_id="cost-12")

            records = load_evidence_records(path)
            latest = load_evidence_file(path)

        self.assertEqual([item.run_id for item in records], ["cost-5", "cost-12"])
        self.assertEqual(latest["experiment"].run_id, "cost-12")

    def test_collect_evidence_cli_records_offline_and_import_checks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "evidence.jsonl"
            stdout = io.StringIO()

            exit_code = main(
                [
                    "collect-evidence",
                    "--evidence-file",
                    str(path),
                    "--offline-tests-detail",
                    "87 tests passed",
                    "--import-check",
                    "duckdb_parquet_runtime:duckdb",
                    "--import-check",
                    "streamlit_dashboard_runtime:streamlit",
                ],
                stdout=stdout,
                import_checker=lambda name: name == "duckdb",
            )
            evidence = load_evidence_file(path)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["ready"])
        self.assertTrue(evidence["offline_tests"].passed)
        self.assertTrue(evidence["duckdb_parquet_runtime"].passed)
        self.assertFalse(evidence["streamlit_dashboard_runtime"].passed)


if __name__ == "__main__":
    unittest.main()

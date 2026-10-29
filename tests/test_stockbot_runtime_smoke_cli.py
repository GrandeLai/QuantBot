import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.audit import RequirementEvidence, load_evidence_file
from quantbot.research_cli import main


class StockBotRuntimeSmokeCliTest(unittest.TestCase):
    def test_runtime_smoke_cli_writes_suite_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            evidence_file = Path(tmpdir) / "evidence.jsonl"
            stdout = io.StringIO()

            exit_code = main(
                ["runtime-smoke", "--evidence-file", str(evidence_file)],
                stdout=stdout,
                runtime_smoke_suite_factory=lambda: FakeRuntimeSmokeSuite(),
            )
            evidence = load_evidence_file(evidence_file)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["ready"])
        self.assertIn("chronos_runtime", payload["failed"])
        self.assertTrue(evidence["duckdb_parquet_runtime"].passed)
        self.assertFalse(evidence["chronos_runtime"].passed)


class FakeRuntimeSmokeSuite:
    def run_all(self):
        return {
            "duckdb_parquet_runtime": RequirementEvidence("duckdb_parquet_runtime", True, "smoke ok"),
            "chronos_runtime": RequirementEvidence("chronos_runtime", False, "model missing"),
        }


if __name__ == "__main__":
    unittest.main()

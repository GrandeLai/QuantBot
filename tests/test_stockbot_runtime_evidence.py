import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.audit import load_evidence_file
from quantbot.research_cli import main
from quantbot.runtime import build_runtime_evidence


class StockBotRuntimeEvidenceTest(unittest.TestCase):
    def test_build_runtime_evidence_maps_packages_to_requirement_ids(self):
        evidence = build_runtime_evidence(import_checker=lambda name: name in {"duckdb", "streamlit"})

        self.assertTrue(evidence["duckdb_parquet_runtime"].passed)
        self.assertTrue(evidence["streamlit_dashboard_runtime"].passed)
        self.assertFalse(evidence["qlib_alpha158_runtime"].passed)
        self.assertFalse(evidence["chronos_runtime"].passed)
        self.assertFalse(evidence["rd_agent_runtime"].passed)
        self.assertFalse(evidence["apscheduler_runtime"].passed)

    def test_runtime_evidence_cli_writes_default_runtime_checks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            evidence_file = Path(tmpdir) / "evidence.jsonl"
            stdout = io.StringIO()

            exit_code = main(
                ["runtime-evidence", "--evidence-file", str(evidence_file)],
                stdout=stdout,
                import_checker=lambda name: name in {"duckdb", "streamlit"},
            )
            evidence = load_evidence_file(evidence_file)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["ready"])
        self.assertIn("qlib_alpha158_runtime", payload["failed"])
        self.assertTrue(evidence["duckdb_parquet_runtime"].passed)
        self.assertFalse(evidence["qlib_alpha158_runtime"].passed)


if __name__ == "__main__":
    unittest.main()

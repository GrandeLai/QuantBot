import io
import importlib.metadata
import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.audit import load_evidence_file
from quantbot.research_cli import main
from quantbot.config_loader import load_runtime_config


class StockBotDeliveryArtifactsTest(unittest.TestCase):
    def test_pyproject_declares_runtime_extras_and_console_script(self):
        pyproject = Path("pyproject.toml").read_text(encoding="utf-8")

        self.assertIn("[project.optional-dependencies]", pyproject)
        self.assertIn("data =", pyproject)
        self.assertIn("live =", pyproject)
        self.assertIn("dashboard =", pyproject)
        self.assertIn('quantbot = "quantbot.cli:main"', pyproject)

    def test_python_metadata_uses_pyproject_as_the_non_stale_source(self):
        pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
        gitignore = Path(".gitignore").read_text(encoding="utf-8")
        generated_metadata = Path("src/quantbot.egg-info/PKG-INFO")

        self.assertIn('requires-python = ">=3.12"', pyproject)
        self.assertIn("*.egg-info/", gitignore)
        if generated_metadata.exists():
            self.assertIn("Requires-Python: >=3.12", generated_metadata.read_text(encoding="utf-8"))
        try:
            installed_metadata = importlib.metadata.metadata("quantbot")
        except importlib.metadata.PackageNotFoundError:
            return
        self.assertEqual(installed_metadata["Requires-Python"], ">=3.12")

    def test_dockerfile_uses_python_312_and_runs_offline_demo_by_default(self):
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

        self.assertIn("FROM python:3.12", dockerfile)
        self.assertIn("pip install", dockerfile)
        self.assertIn(".[data,alpaca]", dockerfile)
        self.assertIn("quantbot", dockerfile)
        self.assertIn("demo", dockerfile)

    def test_example_config_loads_with_env_overrides(self):
        config = load_runtime_config(
            Path("config/quantbot.research.toml"),
            environ={"ALPACA_API_KEY": "key", "ALPACA_SECRET_KEY": "secret"},
        )

        self.assertEqual(config.broker.primary, "Alpaca")
        self.assertEqual(config.broker.api_key, "key")
        self.assertEqual(config.data.root, Path("data"))
        self.assertEqual(config.risk.max_single_stock_weight, 0.05)

    def test_live_smoke_cli_can_append_evidence_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            evidence_path = Path(tmpdir) / "evidence.jsonl"
            stdout = io.StringIO()

            exit_code = main(
                [
                    "live-smoke",
                    "--checks-json",
                    json.dumps([{"name": "alpaca", "passed": True}, {"name": "fred", "passed": True}]),
                    "--evidence-file",
                    str(evidence_path),
                ],
                stdout=stdout,
            )

            evidence = load_evidence_file(evidence_path)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ready"])
        self.assertTrue(evidence["live_api_smoke"].passed)


if __name__ == "__main__":
    unittest.main()

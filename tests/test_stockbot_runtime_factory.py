import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.audit import load_evidence_file
from quantbot.research_cli import main
import quantbot.runtime_factory as runtime_factory_module
from quantbot.runtime_factory import RuntimeSmokeFactory
from quantbot.runtime_smoke import RuntimeSmokeSuite


class StockBotRuntimeFactoryTest(unittest.TestCase):
    def test_research_extra_declares_chronos_runtime_package(self):
        pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn("chronos-forecasting", pyproject)
        self.assertIn("mlflow>=3.13", pyproject)
        self.assertIn("pandas>=2.2,<3", pyproject)

    def test_runtime_factory_builds_suite_from_imported_modules_when_chronos_pipeline_is_provided(self):
        factory = RuntimeSmokeFactory(
            importer=FakeImporter(),
            chronos_pipeline=FakeChronosPipeline([0.01, 0.02, -0.01]),
        )

        suite = factory.build()
        evidence = suite.run_all()

        self.assertIsInstance(suite, RuntimeSmokeSuite)
        self.assertTrue(evidence["duckdb_parquet_runtime"].passed)
        self.assertTrue(evidence["chronos_runtime"].passed)

    def test_runtime_factory_records_clear_chronos_failure_when_model_is_not_explicit(self):
        factory = RuntimeSmokeFactory(importer=FakeImporter(), chronos_pipeline=None, environ={})

        evidence = factory.build().run_all()

        self.assertFalse(evidence["chronos_runtime"].passed)
        self.assertIn("chronos pipeline not configured", evidence["chronos_runtime"].detail)

    def test_runtime_factory_loads_chronos_pipeline_from_model_env(self):
        FakeChronosPipelineModule.ChronosPipeline.loaded = []
        factory = RuntimeSmokeFactory(
            importer=FakeImporter(),
            environ={
                "STOCKBOT_CHRONOS_MODEL_ID": "amazon/chronos-t5-tiny",
                "STOCKBOT_CHRONOS_DEVICE_MAP": "cpu",
            },
        )

        evidence = factory.build().run_all()

        self.assertTrue(evidence["chronos_runtime"].passed)
        self.assertEqual(
            FakeChronosPipelineModule.ChronosPipeline.loaded,
            [("amazon/chronos-t5-tiny", {"device_map": "cpu"})],
        )

    def test_runtime_factory_supports_new_rd_agent_factor_experiment_path(self):
        FakeNewRDAgentModule.QlibFactorExperiment.created = 0
        factory = RuntimeSmokeFactory(
            importer=FakeNewRDAgentImporter(),
            chronos_pipeline=FakeChronosPipeline([0.01, 0.02, -0.01]),
        )

        evidence = factory.build().run_all()

        self.assertTrue(evidence["rd_agent_runtime"].passed)
        self.assertEqual(FakeNewRDAgentModule.QlibFactorExperiment.created, 1)

    def test_runtime_smoke_cli_uses_factory_and_writes_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            evidence_file = Path(tmpdir) / "evidence.jsonl"
            stdout = io.StringIO()

            exit_code = main(
                ["runtime-smoke", "--evidence-file", str(evidence_file)],
                stdout=stdout,
                runtime_smoke_suite_factory=lambda: RuntimeSmokeFactory(
                    importer=FakeImporter(),
                    chronos_pipeline=FakeChronosPipeline([0.01, 0.02, -0.01]),
                ).build(),
            )
            evidence = load_evidence_file(evidence_file)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ready"])
        self.assertTrue(evidence["rd_agent_runtime"].passed)

    def test_runtime_smoke_cli_default_uses_runtime_factory(self):
        original_importlib = runtime_factory_module.importlib
        runtime_factory_module.importlib = FakeImporter()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                evidence_file = Path(tmpdir) / "evidence.jsonl"
                stdout = io.StringIO()

                exit_code = main(["runtime-smoke", "--evidence-file", str(evidence_file)], stdout=stdout)
                evidence = load_evidence_file(evidence_file)
        finally:
            runtime_factory_module.importlib = original_importlib

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["ready"])
        self.assertTrue(evidence["duckdb_parquet_runtime"].passed)
        self.assertFalse(evidence["chronos_runtime"].passed)
        self.assertIn("chronos pipeline not configured", evidence["chronos_runtime"].detail)


class FakeImporter:
    def import_module(self, name):
        modules = {
            "duckdb": FakeDuckDBModule(),
            "qlib": FakeQlib(),
            "qlib.contrib.data.handler": FakeQlibHandlerModule(),
            "rdagent.scenarios.qlib.factor_experiment": FakeRDAgentModule(),
            "streamlit": FakeStreamlit(),
            "apscheduler.schedulers.background": FakeAPSchedulerModule(),
            "chronos": FakeChronosPipelineModule(),
        }
        if name not in modules:
            raise ModuleNotFoundError(name)
        return modules[name]


class FakeNewRDAgentImporter(FakeImporter):
    def import_module(self, name):
        if name == "rdagent.scenarios.qlib.factor_experiment":
            raise ModuleNotFoundError(name)
        if name == "rdagent.scenarios.qlib.experiment.factor_experiment":
            return FakeNewRDAgentModule()
        return super().import_module(name)


class FakeDuckDBModule:
    def connect(self, database=":memory:"):
        return FakeDuckDBConnection()


class FakeDuckDBConnection:
    def register(self, _name, _records):
        pass

    def execute(self, _sql):
        pass


class FakeQlib:
    def init(self, provider_uri, region):
        pass


class FakeQlibHandlerModule:
    Alpha158 = None


class FakeAlphaHandler:
    def fetch(self):
        return {"factor_frame": "ok"}


def _fake_alpha158(**_kwargs):
    return FakeAlphaHandler()


FakeQlibHandlerModule.Alpha158 = staticmethod(_fake_alpha158)


class FakeChronosPipeline:
    def __init__(self, forecast):
        self.forecast = forecast

    def predict(self, _context, prediction_length):
        return self.forecast[:prediction_length]


class FakeChronosPipelineModule:
    class ChronosPipeline:
        loaded = []

        @classmethod
        def from_pretrained(cls, model_id, **kwargs):
            cls.loaded.append((model_id, kwargs))
            return FakeChronosPipeline([0.01, 0.02, -0.01])


class FakeRDAgentModule:
    class QlibFactorExperiment:
        def __init__(self, **_kwargs):
            pass

        def run(self):
            return type("Result", (), {"ic_mean": 0.03, "icir": 0.5, "accepted": True})()


class FakeNewRDAgentModule:
    class QlibFactorExperiment:
        created = 0

        def __init__(self, **_kwargs):
            FakeNewRDAgentModule.QlibFactorExperiment.created += 1


class FakeStreamlit:
    def title(self, _value):
        pass

    def metric(self, _label, _value):
        pass

    def write(self, _value):
        pass


class FakeAPSchedulerModule:
    class BackgroundScheduler:
        def add_job(self, func, trigger, **kwargs):
            self.job = {"func": func, "trigger": trigger, **kwargs}


if __name__ == "__main__":
    unittest.main()

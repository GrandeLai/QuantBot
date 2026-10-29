import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.runtime_smoke import RuntimeSmokeSuite


class StockBotRuntimeSmokeTest(unittest.TestCase):
    def test_runtime_smoke_suite_runs_all_adapter_smokes_with_fakes(self):
        suite = RuntimeSmokeSuite(
            duckdb_connection=FakeDuckDBConnection(),
            qlib_module=FakeQlib(),
            alpha158_factory=FakeAlphaFactory(),
            chronos_pipeline=FakeChronosPipeline([0.01, 0.02, -0.01]),
            rd_agent_experiment_factory=FakeExperiment,
            streamlit_module=FakeStreamlit(),
            scheduler=FakeScheduler(),
        )

        evidence = suite.run_all()

        self.assertTrue(evidence["duckdb_parquet_runtime"].passed)
        self.assertTrue(evidence["qlib_alpha158_runtime"].passed)
        self.assertTrue(evidence["chronos_runtime"].passed)
        self.assertTrue(evidence["rd_agent_runtime"].passed)
        self.assertTrue(evidence["streamlit_dashboard_runtime"].passed)
        self.assertTrue(evidence["apscheduler_runtime"].passed)

    def test_runtime_smoke_suite_records_failure_details(self):
        suite = RuntimeSmokeSuite(
            duckdb_connection=FailingDuckDBConnection(),
            qlib_module=FakeQlib(),
            alpha158_factory=FakeAlphaFactory(),
            chronos_pipeline=FakeChronosPipeline([0.01]),
            rd_agent_experiment_factory=FakeExperiment,
            streamlit_module=FakeStreamlit(),
            scheduler=FakeScheduler(),
        )

        evidence = suite.run_all()

        self.assertFalse(evidence["duckdb_parquet_runtime"].passed)
        self.assertIn("boom", evidence["duckdb_parquet_runtime"].detail)


class FakeDuckDBConnection:
    def register(self, _name, _records):
        pass

    def execute(self, _sql):
        pass


class FailingDuckDBConnection:
    def register(self, _name, _records):
        raise RuntimeError("boom")

    def execute(self, _sql):
        pass


class FakeQlib:
    def init(self, provider_uri, region):
        self.provider_uri = provider_uri
        self.region = region


class FakeAlphaFactory:
    def __call__(self, **_kwargs):
        return FakeAlphaHandler()


class FakeAlphaHandler:
    def fetch(self):
        return {"factor_frame": "ok"}


class FakeChronosPipeline:
    def __init__(self, forecast):
        self.forecast = forecast

    def predict(self, _context, prediction_length):
        return self.forecast[:prediction_length]


class FakeExperiment:
    def __init__(self, **_kwargs):
        pass

    def run(self):
        return type("Result", (), {"ic_mean": 0.03, "icir": 0.5, "accepted": True})()


class FakeStreamlit:
    def title(self, _value):
        pass

    def metric(self, _label, _value):
        pass

    def write(self, _value):
        pass


class FakeScheduler:
    def add_job(self, func, trigger, **kwargs):
        self.job = {"func": func, "trigger": trigger, **kwargs}


if __name__ == "__main__":
    unittest.main()

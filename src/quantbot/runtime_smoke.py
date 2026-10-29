"""Runtime smoke checks that exercise integration adapters."""

from pathlib import Path
from tempfile import TemporaryDirectory

from .audit import RequirementEvidence
from .dashboard_rendering import StreamlitDashboardRenderer
from .data.duckdb_store import DuckDBParquetStore
from .integrations.chronos import ChronosAdapter
from .integrations.qlib import QlibAlpha158Adapter
from .integrations.scheduler import APSchedulerRegistrar


class RuntimeSmokeSuite:
    def __init__(
        self,
        duckdb_connection,
        qlib_module,
        alpha158_factory,
        chronos_pipeline,
        rd_agent_experiment_factory,
        streamlit_module,
        scheduler,
    ):
        self.duckdb_connection = duckdb_connection
        self.qlib_module = qlib_module
        self.alpha158_factory = alpha158_factory
        self.chronos_pipeline = chronos_pipeline
        self.rd_agent_experiment_factory = rd_agent_experiment_factory
        self.streamlit_module = streamlit_module
        self.scheduler = scheduler

    def run_all(self):
        checks = {
            "duckdb_parquet_runtime": self._smoke_duckdb,
            "qlib_alpha158_runtime": self._smoke_qlib,
            "chronos_runtime": self._smoke_chronos,
            "rd_agent_runtime": self._smoke_rd_agent,
            "streamlit_dashboard_runtime": self._smoke_streamlit,
            "apscheduler_runtime": self._smoke_apscheduler,
        }
        return {
            requirement_id: _run_check(requirement_id, check)
            for requirement_id, check in checks.items()
        }

    def _smoke_duckdb(self):
        with TemporaryDirectory() as tmpdir:
            store = DuckDBParquetStore(Path(tmpdir), self.duckdb_connection)
            store.write_partition("smoke", "2024-01-02", [{"ticker": "AAPL", "close": 100}])

    def _smoke_qlib(self):
        QlibAlpha158Adapter(self.qlib_module, self.alpha158_factory).compute(
            provider_uri="~/.qlib/us_data",
            instruments="sp500",
            start_time="2024-01-01",
            end_time="2024-01-31",
        )

    def _smoke_chronos(self):
        ChronosAdapter(self.chronos_pipeline).predict_features([0.0, 0.01, -0.01], prediction_length=3)

    def _smoke_rd_agent(self):
        experiment = self.rd_agent_experiment_factory(
            factor_description="construction check only",
            market="us_sp500",
            validation_period=("2020-01-01", "2020-12-31"),
        )
        if experiment is None:
            raise ValueError("RD-Agent experiment construction returned no object")
        return "construction_only; no research run, model call or factor performance evaluated"

    def _smoke_streamlit(self):
        StreamlitDashboardRenderer(self.streamlit_module).render(
            {
                "regime": "R1_BULL_LOW_VOL",
                "gross_exposure": 0.5,
                "performance": {},
                "factor_exposure": {},
                "top_positions": [],
            }
        )

    def _smoke_apscheduler(self):
        APSchedulerRegistrar(self.scheduler).register_daily_after_close(lambda: None)


def _run_check(requirement_id, check):
    try:
        detail = check()
    except Exception as error:
        return RequirementEvidence(requirement_id, False, "%s: %s" % (type(error).__name__, error))
    return RequirementEvidence(requirement_id, True, detail or "smoke ok")

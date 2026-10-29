"""Factory for production runtime smoke suites."""

import importlib
import os

from .audit import RequirementEvidence
from .runtime import DEFAULT_RUNTIME_REQUIREMENTS
from .runtime_smoke import RuntimeSmokeSuite


class RuntimeSmokeFactory:
    def __init__(self, importer=None, chronos_pipeline=None, environ=None):
        self.importer = importer or importlib
        self.chronos_pipeline = chronos_pipeline
        self.environ = environ if environ is not None else os.environ

    def build(self):
        try:
            duckdb = self.importer.import_module("duckdb")
            qlib = self.importer.import_module("qlib")
            qlib_handler = self.importer.import_module("qlib.contrib.data.handler")
            rd_experiment_factory = self._rd_agent_experiment_factory()
            streamlit = self.importer.import_module("streamlit")
            apscheduler = self.importer.import_module("apscheduler.schedulers.background")
            chronos_pipeline = self.chronos_pipeline or self._chronos_pipeline_from_config()
            return RuntimeSmokeSuite(
                duckdb_connection=duckdb.connect(database=":memory:"),
                qlib_module=qlib,
                alpha158_factory=qlib_handler.Alpha158,
                chronos_pipeline=chronos_pipeline,
                rd_agent_experiment_factory=rd_experiment_factory,
                streamlit_module=streamlit,
                scheduler=apscheduler.BackgroundScheduler(),
            )
        except Exception as error:
            return _FactoryFailureSmokeSuite(error)

    def _chronos_pipeline_from_config(self):
        model_id = self.environ.get("STOCKBOT_CHRONOS_MODEL_ID", "")
        if not model_id:
            return _MissingChronosPipeline()
        chronos = self.importer.import_module("chronos")
        kwargs = {}
        device_map = self.environ.get("STOCKBOT_CHRONOS_DEVICE_MAP", "cpu")
        if device_map:
            kwargs["device_map"] = device_map
        torch_dtype = self.environ.get("STOCKBOT_CHRONOS_TORCH_DTYPE", "")
        if torch_dtype:
            torch = self.importer.import_module("torch")
            kwargs["torch_dtype"] = getattr(torch, torch_dtype)
        return chronos.ChronosPipeline.from_pretrained(model_id, **kwargs)

    def _rd_agent_experiment_factory(self):
        module_names = (
            "rdagent.scenarios.qlib.factor_experiment",
            "rdagent.scenarios.qlib.experiment.factor_experiment",
        )
        last_error = None
        for module_name in module_names:
            try:
                module = self.importer.import_module(module_name)
                return _RDAgentSmokeExperimentFactory(module.QlibFactorExperiment)
            except ModuleNotFoundError as error:
                last_error = error
        raise last_error


class _MissingChronosPipeline:
    def predict(self, _context, prediction_length):
        del prediction_length
        raise RuntimeError("chronos pipeline not configured; pass an explicit model-backed pipeline")


class _RDAgentSmokeExperimentFactory:
    def __init__(self, experiment_class):
        self.experiment_class = experiment_class

    def __call__(self, **kwargs):
        # RD-Agent 0.8 exposes QlibFactorExperiment as a data object, while
        # older examples exposed a run() method. Smoke tests should verify the
        # installed class can be imported/constructed without launching an LLM
        # research loop.
        return self.experiment_class(sub_tasks=[])


class _FactoryFailureSmokeSuite:
    def __init__(self, error):
        self.error = error

    def run_all(self):
        return {
            requirement.requirement_id: RequirementEvidence(
                requirement.requirement_id,
                False,
                "runtime factory failed: %s: %s" % (type(self.error).__name__, self.error),
            )
            for requirement in DEFAULT_RUNTIME_REQUIREMENTS
        }

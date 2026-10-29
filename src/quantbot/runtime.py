"""Runtime dependency evidence for completion audits."""

import importlib.util
from dataclasses import dataclass
from typing import Callable, Dict, Sequence

from .audit import RequirementEvidence


@dataclass(frozen=True)
class RuntimeRequirement:
    requirement_id: str
    package_name: str


DEFAULT_RUNTIME_REQUIREMENTS: Sequence[RuntimeRequirement] = (
    RuntimeRequirement("duckdb_parquet_runtime", "duckdb"),
    RuntimeRequirement("qlib_alpha158_runtime", "qlib"),
    RuntimeRequirement("chronos_runtime", "chronos"),
    RuntimeRequirement("rd_agent_runtime", "rdagent"),
    RuntimeRequirement("streamlit_dashboard_runtime", "streamlit"),
    RuntimeRequirement("apscheduler_runtime", "apscheduler"),
)


def build_runtime_evidence(import_checker: Callable[[str], bool] = None) -> Dict[str, RequirementEvidence]:
    checker = import_checker or _can_import
    evidence = {}
    for requirement in DEFAULT_RUNTIME_REQUIREMENTS:
        passed = bool(checker(requirement.package_name))
        evidence[requirement.requirement_id] = RequirementEvidence(
            requirement.requirement_id,
            passed=passed,
            detail="%s import %s" % (requirement.package_name, "ok" if passed else "missing"),
        )
    return evidence


def _can_import(package_name: str) -> bool:
    return importlib.util.find_spec(package_name) is not None

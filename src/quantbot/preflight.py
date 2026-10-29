"""Runtime preflight checks for live dependencies."""

import importlib.util
import os
from dataclasses import dataclass
from typing import Callable, Dict, List


REQUIRED_ENV = (
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
)

OPTIONAL_PACKAGES = (
    "duckdb",
    "qlib",
    "chronos",
    "rdagent",
    "apscheduler",
    "streamlit",
)


@dataclass(frozen=True)
class PreflightReport:
    ready: bool
    missing_env: List[str]
    missing_packages: List[str]


class RuntimePreflight:
    def __init__(
        self,
        environ: Dict[str, str] = None,
        import_checker: Callable[[str], bool] = None,
    ):
        self.environ = environ if environ is not None else os.environ
        self.import_checker = import_checker or _can_import

    def check(self) -> PreflightReport:
        missing_env = [name for name in REQUIRED_ENV if not self.environ.get(name)]
        missing_packages = [
            name for name in OPTIONAL_PACKAGES
            if not self.import_checker(name)
        ]
        return PreflightReport(
            ready=not missing_env and not missing_packages,
            missing_env=missing_env,
            missing_packages=missing_packages,
        )


def _can_import(name: str) -> bool:
    return importlib.util.find_spec(name) is not None

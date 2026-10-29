"""Live dependency smoke-test gates."""

from dataclasses import dataclass
from typing import Callable, List, Sequence


@dataclass(frozen=True)
class LiveSmokeCheck:
    name: str
    check: Callable[[], bool]


@dataclass(frozen=True)
class LiveSmokeResult:
    ready: bool
    passed: List[str]
    failed: List[str]


class LiveSmokeGate:
    def __init__(self, checks: Sequence[LiveSmokeCheck]):
        self.checks = list(checks)

    def run(self) -> LiveSmokeResult:
        passed = []
        failed = []
        for check in self.checks:
            try:
                ok = bool(check.check())
            except Exception:
                ok = False
            if ok:
                passed.append(check.name)
            else:
                failed.append(check.name)
        return LiveSmokeResult(ready=not failed, passed=passed, failed=failed)

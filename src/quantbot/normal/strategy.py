"""Daily-strategy adapter for the shared trading spine."""

from __future__ import annotations

from types import MappingProxyType

from ..trading import PointInTimeSnapshot
from .domain import NormalAllocationRequest
from .portfolio import NormalPortfolioAllocator


class PointInTimeNormalStrategy:
    def __init__(self, provider, allocator: NormalPortfolioAllocator | None = None):
        if not hasattr(provider, "build_request"):
            raise TypeError("provider must expose build_request(snapshot, current_weights)")
        self.provider = provider
        self.allocator = allocator or NormalPortfolioAllocator()
        self._current_weights = MappingProxyType({})
        self.last_report = None
        self._last_snapshot = None

    def generate_targets(self, snapshot: PointInTimeSnapshot):
        if not isinstance(snapshot, PointInTimeSnapshot):
            raise TypeError("snapshot must be a PointInTimeSnapshot")
        if snapshot.security_master is None:
            raise ValueError("normal strategy requires a point-in-time security master")
        if self._last_snapshot is snapshot and self.last_report is not None:
            return self.last_report.targets
        request = self.provider.build_request(snapshot, self._current_weights)
        if not isinstance(request, NormalAllocationRequest):
            raise TypeError("provider must return NormalAllocationRequest")
        if request.security_master != snapshot.security_master:
            raise ValueError("provider request must use the event security master")
        report = self.allocator.allocate(request)
        self.last_report = report
        self._last_snapshot = snapshot
        self._current_weights = MappingProxyType(dict(report.targets.weights))
        return report.targets

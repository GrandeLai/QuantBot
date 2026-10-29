"""Candidate factor pool construction with source diversity."""

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Sequence

from .evaluation import FactorMetrics, passes_admission


DEFAULT_SOURCE_SHARES = {
    "alpha158": 0.40,
    "kronos": 0.30,
    "differentiated": 0.30,
}

DEFAULT_SOURCE_GROUPS = {
    "alpha158": ("alpha158",),
    "kronos": ("kronos",),
    "differentiated": ("rd_agent", "custom", "self_developed"),
}


@dataclass(frozen=True)
class CandidateFactor:
    name: str
    source: str
    metrics: FactorMetrics


@dataclass(frozen=True)
class CandidatePool:
    factors: List[CandidateFactor]

    def source_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for factor in self.factors:
            counts[factor.source] = counts.get(factor.source, 0) + 1
        return counts

    @property
    def average_icir(self) -> float:
        if not self.factors:
            return 0.0
        return sum(factor.metrics.icir for factor in self.factors) / len(self.factors)


class CandidatePoolBuilder:
    def __init__(
        self,
        target_size: int = 30,
        source_shares: Mapping[str, float] = None,
        source_groups: Mapping[str, Sequence[str]] = None,
    ):
        self.target_size = target_size
        self.source_shares = dict(source_shares or DEFAULT_SOURCE_SHARES)
        self.source_groups = {
            source_group: tuple(sources)
            for source_group, sources in (source_groups or DEFAULT_SOURCE_GROUPS).items()
        }

    def build(self, factors: Iterable[CandidateFactor]) -> CandidatePool:
        admitted = [factor for factor in factors if passes_admission(factor.metrics)]
        by_source: Dict[str, List[CandidateFactor]] = {}
        for factor in admitted:
            by_source.setdefault(factor.source, []).append(factor)
        for source in by_source:
            by_source[source].sort(key=lambda factor: (-factor.metrics.icir, factor.name))

        selected: List[CandidateFactor] = []
        selected_names = set()
        for source_group, share in self.source_shares.items():
            quota = int(round(self.target_size * share))
            group_selected = 0
            for factor in self._group_factors(source_group, by_source):
                if factor.name in selected_names:
                    continue
                selected.append(factor)
                selected_names.add(factor.name)
                group_selected += 1
                if group_selected >= quota:
                    break

        if len(selected) < self.target_size:
            remainder = sorted(
                [factor for factor in admitted if factor.name not in selected_names],
                key=lambda factor: (-factor.metrics.icir, factor.name),
            )
            selected.extend(remainder[: self.target_size - len(selected)])

        selected = selected[: self.target_size]
        return CandidatePool(factors=selected)

    def _group_factors(
        self,
        source_group: str,
        by_source: Mapping[str, List[CandidateFactor]],
    ) -> List[CandidateFactor]:
        sources = self.source_groups.get(source_group, (source_group,))
        factors = []
        for source in sources:
            factors.extend(by_source.get(source, []))
        return sorted(factors, key=lambda factor: (-factor.metrics.icir, factor.name))

"""Historical replay helpers for acceptance-log plumbing validation."""

from dataclasses import dataclass
from typing import Dict, List, Sequence

from .live import LiveEquitySnapshot, LiveTradingRun, rollup_live_snapshots
from .oos_backtest import DailyReturnPoint
from .paper import PaperAccountSnapshot, PaperTradingRun, paper_period_for_timestamp, rollup_paper_snapshots


@dataclass(frozen=True)
class HistoricalReplayResult:
    paper_snapshots: List[PaperAccountSnapshot]
    paper_runs: List[PaperTradingRun]
    live_snapshots: List[LiveEquitySnapshot]
    live_sessions: List[LiveTradingRun]
    backtest_returns_by_period: Dict[str, float]
    skipped_paper_periods: List[str]
    skipped_live_days: List[str]


def build_historical_replay(
    daily_returns: Sequence[DailyReturnPoint],
    initial_equity: float = 100_000.0,
    snapshot_time: str = "21:00:00Z",
    min_paper_snapshots_per_period: int = 2,
    min_live_snapshots_per_day: int = 1,
) -> HistoricalReplayResult:
    if initial_equity <= 0:
        raise ValueError("initial_equity must be positive")

    paper_snapshots: List[PaperAccountSnapshot] = []
    live_snapshots: List[LiveEquitySnapshot] = []
    equity = float(initial_equity)
    for point in sorted(daily_returns, key=lambda item: item.trading_date):
        equity *= 1.0 + float(point.portfolio_return)
        timestamp = "%sT%s" % (point.trading_date.isoformat(), snapshot_time)
        paper_snapshots.append(
            PaperAccountSnapshot(
                timestamp=timestamp,
                equity=equity,
                errors=0,
                notes="historical-replay",
            )
        )
        live_snapshots.append(
            LiveEquitySnapshot(
                timestamp=timestamp,
                equity=equity,
                errors=0,
                notes="historical-replay",
            )
        )

    backtest_returns = _period_returns_from_snapshots(paper_snapshots)
    paper_rollup = rollup_paper_snapshots(
        paper_snapshots,
        backtest_returns=backtest_returns,
        min_snapshots_per_period=min_paper_snapshots_per_period,
    )
    live_rollup = rollup_live_snapshots(
        live_snapshots,
        baseline_equity=initial_equity,
        min_snapshots_per_day=min_live_snapshots_per_day,
    )

    return HistoricalReplayResult(
        paper_snapshots=paper_snapshots,
        paper_runs=paper_rollup.runs,
        live_snapshots=live_snapshots,
        live_sessions=live_rollup.sessions,
        backtest_returns_by_period=backtest_returns,
        skipped_paper_periods=paper_rollup.skipped_periods,
        skipped_live_days=live_rollup.skipped_days,
    )


def _period_returns_from_snapshots(snapshots: Sequence[PaperAccountSnapshot]) -> Dict[str, float]:
    grouped: Dict[str, List[PaperAccountSnapshot]] = {}
    for snapshot in sorted(snapshots, key=lambda item: item.timestamp):
        grouped.setdefault(paper_period_for_timestamp(snapshot.timestamp), []).append(snapshot)

    returns: Dict[str, float] = {}
    for period, period_snapshots in grouped.items():
        if len(period_snapshots) < 2:
            continue
        start = period_snapshots[0].equity
        end = period_snapshots[-1].equity
        if start > 0:
            returns[period] = end / start - 1.0
    return returns

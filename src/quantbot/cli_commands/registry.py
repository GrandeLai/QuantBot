"""Explicit parser registration and handler binding for every CLI command."""

import argparse
from dataclasses import dataclass
from typing import Callable

from ..daytrade import DEFAULT_DAYTRADE_SYMBOLS
from ..moomoo_paper import DEFAULT_MOOMOO_CANARY_SYMBOLS
from ..oos_backtest import DEFAULT_BENCHMARK, DEFAULT_OOS_SYMBOLS
from .acceptance import handle_acceptance_cycle
from .daytrade import (
    handle_daytrade_backtest_run,
    handle_daytrade_cache_fill,
    handle_daytrade_compare_configs,
    handle_daytrade_live_cycle,
    handle_daytrade_optimize_run,
    handle_daytrade_paper_cycle,
    handle_daytrade_schedule,
    handle_daytrade_status,
)
from .moomoo import handle_moomoo_simulate
from .moomoo_paper import handle_moomoo_paper_canary
from .general import (
    handle_acceptance_schedule,
    handle_collect_evidence,
    handle_completion_audit,
    handle_live_smoke,
    handle_preflight,
    handle_runtime_evidence,
    handle_runtime_smoke,
    handle_schedule,
)
from .live import (
    handle_live_soak_evidence,
    handle_live_soak_record,
    handle_live_soak_rollup_snapshots,
    handle_live_soak_snapshot_record,
    handle_live_soak_status,
)
from .normal import (
    handle_backtest_acceptance,
    handle_historical_replay,
    handle_normal_experiment_run,
    handle_oos_backtest_run,
)
from .paper import (
    handle_paper_acceptance,
    handle_paper_evidence,
    handle_paper_rollup_snapshots,
    handle_paper_run_record,
    handle_paper_snapshot_record,
    handle_paper_status,
)
from .readiness import handle_strategy_readiness
from .reporting import handle_experiment_acceptance_report


ParserConfigurer = Callable[[argparse.ArgumentParser], None]


@dataclass(frozen=True)
class CommandSpec:
    name: str
    handler: Callable
    configure: ParserConfigurer


def _noop(_parser):
    return None


def _preflight(parser):
    parser.add_argument("--json", action="store_true", dest="as_json")


def _strategy_readiness(parser):
    parser.add_argument(
        "--strategy",
        choices=("normal-h1", "normal-h2", "daytrade-h3", "daytrade-h9"),
        required=True,
    )
    parser.add_argument(
        "--access-mode",
        choices=("research", "adaptive_development", "sealed"),
        required=True,
    )
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--warmup-start", default="")
    parser.add_argument("--warmup-end", default="")
    parser.add_argument("--bundle-root", default="")
    parser.add_argument("--adapter", default="")
    parser.add_argument("--universe-manifest", default="")
    parser.add_argument("--cache-dir", default="")
    parser.add_argument("--exposure-ledger", default="")


def _acceptance_schedule(parser):
    parser.add_argument("--evidence-file", default="data/evidence.jsonl")
    parser.add_argument("--paper-snapshot-log", default="data/paper_snapshots.jsonl")
    parser.add_argument("--paper-runs-log", default="data/paper_runs.jsonl")
    parser.add_argument("--live-snapshot-log", default="data/live_snapshots.jsonl")
    parser.add_argument("--live-runs-log", default="data/live_runs.jsonl")
    parser.add_argument("--json", action="store_true", dest="as_json")


def _paper_acceptance(parser):
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--runs-json")
    source.add_argument("--runs-log")


def _paper_run_record(parser):
    parser.add_argument("--log", required=True)
    parser.add_argument("--period", required=True)
    parser.add_argument("--errors", type=int, required=True)
    parser.add_argument("--paper-return", type=float, required=True)
    parser.add_argument("--backtest-return", type=float, required=True)


def _account_snapshot_record(parser):
    parser.add_argument("--log", required=True)
    parser.add_argument("--timestamp")
    parser.add_argument("--equity", type=float)
    parser.add_argument("--errors", type=int, default=0)
    parser.add_argument("--notes", default="")
    parser.add_argument("--alpaca-endpoint")


def _paper_rollup(parser):
    parser.add_argument("--snapshot-log", required=True)
    parser.add_argument("--runs-log", required=True)
    parser.add_argument("--backtest-returns-json", required=True)
    parser.add_argument("--min-snapshots-per-period", type=int, default=2)


def _paper_evidence(parser):
    parser.add_argument("--log", required=True)
    parser.add_argument("--evidence-file", required=True)


def _paper_status(parser):
    parser.add_argument("--log", required=True)
    parser.add_argument("--min-weeks", type=int, default=4)
    parser.add_argument("--max-performance-deviation", type=float, default=0.30)


def _live_smoke(parser):
    parser.add_argument("--checks-json", required=True)
    parser.add_argument("--evidence-file")


def _completion_audit(parser):
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--evidence-json")
    source.add_argument("--evidence-file")


def _experiment_acceptance_report(parser):
    parser.add_argument("--report-file", required=True)
    parser.add_argument("--baseline-report-file", default="")
    parser.add_argument("--output-file", default="")


def _collect_evidence(parser):
    parser.add_argument("--evidence-file", required=True)
    parser.add_argument("--offline-tests-detail")
    parser.add_argument("--import-check", action="append", default=[])


def _evidence_file(parser):
    parser.add_argument("--evidence-file", required=True)


def _backtest_acceptance(parser):
    parser.add_argument("--summary-json", required=True)
    parser.add_argument("--annual-turnover", type=float, required=True)
    parser.add_argument("--two-way-cost-rate", type=float, default=0.0015)
    parser.add_argument(
        "--summary-includes-costs",
        "--summary-net-of-cost",
        action="store_true",
        dest="summary_includes_costs",
    )
    parser.add_argument("--evidence-file", required=True)


def _add_oos_data_args(parser):
    parser.add_argument("--data-source", choices=("auto", "alpaca", "yfinance"), default="auto")
    parser.add_argument("--symbols", default=",".join(DEFAULT_OOS_SYMBOLS))
    parser.add_argument("--benchmark", default=DEFAULT_BENCHMARK)
    parser.add_argument("--data-start", default="2014-01-01")
    parser.add_argument("--data-end", default="2025-01-01")
    parser.add_argument("--alpaca-data-url")
    parser.add_argument("--alpaca-feed", default="")
    parser.add_argument("--config", default="")
    parser.add_argument("--train-start", default="2015-01-01")
    parser.add_argument("--train-end", default="2022-12-31")
    parser.add_argument("--validation-start", default="2023-01-01")
    parser.add_argument("--validation-end", default="2024-12-31")
    parser.add_argument("--lookback-days", type=int, default=252)
    parser.add_argument("--top-n", type=int, default=5)


def _oos_backtest(parser):
    parser.add_argument("--evidence-file", required=True)
    _add_oos_data_args(parser)
    parser.add_argument("--two-way-cost-rate", type=float, default=0.0015)


def _historical_replay(parser):
    parser.add_argument("--evidence-file", required=True)
    parser.add_argument("--paper-snapshot-log", required=True)
    parser.add_argument("--paper-runs-log", required=True)
    parser.add_argument("--live-snapshot-log", required=True)
    parser.add_argument("--live-runs-log", required=True)
    parser.add_argument("--initial-equity", type=float, default=100_000.0)
    _add_oos_data_args(parser)
    parser.add_argument("--min-paper-weeks", type=int, default=4)
    parser.add_argument("--max-paper-performance-deviation", type=float, default=0.30)
    parser.add_argument("--min-live-sessions", type=int, default=20)
    parser.add_argument("--max-live-drawdown", type=float, default=0.15)


def _normal_experiment(parser):
    parser.add_argument("--config", default="")
    parser.add_argument(
        "--experiment-id",
        choices=(
            "normal-h1-quality-diversification",
            "normal-h2-capital-efficiency",
        ),
        default="normal-h1-quality-diversification",
    )
    parser.add_argument("--bundle-root", default="")
    parser.add_argument(
        "--adapter",
        default="",
        help=(
            "deployment adapter as module:callable; alternatively set "
            "STOCKBOT_NORMAL_ADAPTER"
        ),
    )
    parser.add_argument("--exposure-ledger", required=True)
    parser.add_argument("--evidence-file", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--access-mode",
        choices=("research", "adaptive_development", "fixed_holdout", "sealed"),
        required=True,
    )
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--warmup-start", required=True)
    parser.add_argument("--warmup-end", required=True)
    parser.add_argument("--initial-equity", type=float, default=100_000.0)
    parser.add_argument("--cost-bps-per-side", type=float, default=5.0)
    parser.add_argument("--stress-cost-bps-per-side", type=float, default=10.0)
    parser.add_argument("--output-file", default="")
    parser.add_argument("--historical-audit-replay", action="store_true")
    parser.add_argument(
        "--historical-trial-artifact",
        default="",
        help=(
            "signed completed/rejected Normal H2 audit artifact; alternatively "
            "set STOCKBOT_NORMAL_H2_AUDIT_ARTIFACT_PATH"
        ),
    )
    parser.add_argument(
        "--adaptive-validation-artifact",
        default="",
        help=(
            "signed frozen Normal adaptive validation artifact for sealed H1; "
            "alternatively set STOCKBOT_NORMAL_ADAPTIVE_VALIDATION_ARTIFACT_PATH"
        ),
    )


def _daytrade_schedule(parser):
    parser.add_argument("--trades-log", default="data/daytrade_trades.jsonl")
    parser.add_argument("--daily-log", default="data/daytrade_daily.jsonl")
    parser.add_argument("--evidence-file", default="data/daytrade_evidence.jsonl")
    parser.add_argument("--json", action="store_true", dest="as_json")


def _add_daytrade_data_args(parser, cache_default: str = ""):
    parser.add_argument("--data-source", choices=("auto", "alpaca", "polygon"), default="auto")
    parser.add_argument("--symbols", default=",".join(DEFAULT_DAYTRADE_SYMBOLS))
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--cache-dir", default=cache_default)
    parser.add_argument("--alpaca-data-url")
    parser.add_argument("--alpaca-feed", default="")
    parser.add_argument("--config", default="")
    parser.add_argument("--polygon-url")
    parser.add_argument("--expected-sessions-json")
    parser.add_argument("--universe-manifest", default="")
    parser.add_argument("--exposure-ledger", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument(
        "--access-mode",
        choices=("research", "adaptive_development", "fixed_holdout", "sealed"),
        default="",
    )
    parser.add_argument("--warmup-start", default="")
    parser.add_argument("--dataset-hash", default="")
    parser.add_argument("--config-hash", default="")
    parser.add_argument("--cost-model-hash", default="")
    parser.add_argument("--code-hash", default="")


def _add_daytrade_config_args(parser):
    parser.add_argument("--account-equity", type=float, default=100_000.0)
    parser.add_argument("--capital-fraction", type=float, default=0.10)
    parser.add_argument("--per-trade-risk-fraction", type=float, default=0.0035)
    parser.add_argument("--daily-loss-limit-fraction", type=float, default=0.01)
    parser.add_argument("--max-trades-per-day", type=int, default=6)
    parser.add_argument("--max-concurrent-positions", type=int, default=3)
    parser.add_argument("--max-losing-trades-per-day", type=int, default=0)
    parser.add_argument("--max-consecutive-losing-trades-per-day", type=int, default=0)
    parser.add_argument("--volume-multiplier", type=float, default=1.0)
    parser.add_argument("--min-relative-volume", type=float, default=0.0)
    parser.add_argument("--cost-bps", type=float, default=0.0)
    parser.add_argument("--max-volume-participation", type=float, default=1.0)
    parser.add_argument("--symbol-filter", choices=("none", "opening-range"), default="none")
    parser.add_argument("--max-active-symbols", type=int, default=10)
    parser.add_argument("--min-opening-range-pct", type=float, default=0.0)
    parser.add_argument("--min-opening-dollar-volume", type=float, default=0.0)
    parser.add_argument("--require-positive-opening-momentum", action="store_true")
    parser.add_argument("--entry-end", default="10:30")
    parser.add_argument("--breakeven-after-r", type=float, default=0.0)
    parser.add_argument("--trailing-stop-after-r", type=float, default=0.0)
    parser.add_argument("--max-holding-minutes", type=float, default=0.0)
    parser.add_argument("--vwap-exit-bars", type=int, default=3)
    parser.add_argument("--min-directional-spy-return", type=float, default=-1.0)
    parser.add_argument("--min-directional-qqq-return", type=float, default=-1.0)
    parser.add_argument("--min-directional-sector-strength", type=float, default=-1.0)
    parser.add_argument("--dynamic-risk-sizing", action="store_true")
    parser.add_argument("--min-sessions", type=int, default=20)
    parser.add_argument("--max-drawdown", type=float, default=0.05)


def _daytrade_cache(parser):
    _add_daytrade_data_args(parser, cache_default="data/daytrade_cache")


def _daytrade_backtest(parser):
    _add_daytrade_data_args(parser)
    _add_daytrade_config_args(parser)
    parser.add_argument("--trades-log", required=True)
    parser.add_argument("--daily-log", required=True)
    parser.add_argument("--evidence-file", required=True)


def _daytrade_optimize(parser):
    _add_daytrade_data_args(parser, cache_default="data/daytrade_cache")
    _add_daytrade_config_args(parser)
    parser.add_argument("--train-months", type=int, default=3)
    parser.add_argument("--validation-months", type=int, default=1)
    parser.add_argument("--max-candidates", type=int, default=None)
    parser.add_argument(
        "--experiment-set",
        choices=(
            "h1-h4",
            "h3-rvol",
            "h6-market",
            "h7-rvol-window",
            "h8-rvol-risk",
            "h9-rvol-reward-risk",
        ),
        default="h1-h4",
    )
    parser.add_argument(
        "--historical-audit-replay",
        action="store_true",
        help=(
            "allow a retired experiment set to be replayed for immutable "
            "historical-trial backfill only"
        ),
    )
    _add_daytrade_feature_args(parser)
    parser.add_argument("--output-file", required=True)
    parser.add_argument("--evidence-file", required=True)
    parser.add_argument(
        "--adaptive-validation-artifact",
        default="",
        help=(
            "signed 2023-2024 H9 survival artifact required only by sealed H9; "
            "alternatively set STOCKBOT_DAYTRADE_ADAPTIVE_VALIDATION_ARTIFACT_PATH"
        ),
    )


def _daytrade_compare(parser):
    _add_daytrade_data_args(parser, cache_default="data/daytrade_cache")
    _add_daytrade_config_args(parser)
    _add_daytrade_feature_args(parser)
    parser.add_argument("--walk-forward-config-json", required=True)
    parser.add_argument("--holdout-manifest", default="")
    parser.add_argument(
        "--historical-audit-replay",
        action="store_true",
        help=(
            "allow comparison of a retired experiment for historical audit "
            "only; fixed-holdout and sealed access remain forbidden"
        ),
    )
    parser.add_argument("--stress-cost-bps", type=float)
    parser.add_argument("--output-file")


def _add_daytrade_feature_args(parser):
    parser.add_argument("--feature-warmup-days", type=int, default=60)
    parser.add_argument("--feature-observation-end", default="")
    parser.add_argument("--sector-proxies-json", default="")


def _daytrade_paper(parser):
    _add_daytrade_data_args(parser)
    _add_daytrade_config_args(parser)
    parser.add_argument("--trades-log", default="data/daytrade_paper_trades.jsonl")
    parser.add_argument("--daily-log", default="data/daytrade_paper_daily.jsonl")
    parser.add_argument("--evidence-file", default="data/daytrade_paper_evidence.jsonl")


def _daytrade_live(parser):
    _add_daytrade_data_args(parser)
    _add_daytrade_config_args(parser)
    parser.add_argument("--trades-log", default="data/daytrade_live_trades.jsonl")
    parser.add_argument("--daily-log", default="data/daytrade_live_daily.jsonl")
    parser.add_argument("--evidence-file", default="data/daytrade_live_evidence.jsonl")
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--submit-orders", action="store_true")
    parser.add_argument("--live-alpaca-endpoint")


def _daytrade_status(parser):
    parser.add_argument("--trades-log", required=True)
    parser.add_argument("--daily-log", required=True)
    parser.add_argument("--min-sessions", type=int, default=20)
    parser.add_argument("--max-drawdown", type=float, default=0.05)


def _moomoo_simulate(parser):
    parser.add_argument(
        "action",
        choices=("snapshot", "place", "query", "cancel", "reconcile"),
        help="operation against a moomoo simulated-trading account",
    )
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int)
    parser.add_argument("--acc-id")
    parser.add_argument("--security-firm", choices=("FUTUINC",), default="")
    parser.add_argument("--code")
    parser.add_argument("--side", choices=("BUY", "SELL"))
    parser.add_argument("--quantity", type=int)
    parser.add_argument("--price", type=float)
    parser.add_argument("--order-id")
    parser.add_argument("--submission-marker")
    parser.add_argument(
        "--confirm-simulated-action",
        action="store_true",
        help="acknowledge that place/cancel changes the shared paper account",
    )


def _moomoo_paper_canary(parser):
    parser.add_argument("--symbols", default=",".join(DEFAULT_MOOMOO_CANARY_SYMBOLS))
    parser.add_argument("--capital-fraction", type=float, default=0.10)
    parser.add_argument(
        "--flatten-minutes-before-close",
        type=int,
        default=10,
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=5.0,
        help="Broker reconciliation interval; minimum 5 seconds.",
    )
    parser.add_argument("--max-quote-age-seconds", type=float, default=5.0)
    parser.add_argument(
        "--marketable-limit-buffer-bps",
        type=float,
        default=50.0,
    )
    parser.add_argument(
        "--state-file",
        default="data/moomoo_paper_canary_state.json",
    )
    parser.add_argument(
        "--lock-file",
        default="data/moomoo_paper_canary_state.lock",
    )
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int)
    parser.add_argument("--acc-id")
    parser.add_argument("--security-firm", choices=("FUTUINC",), default="")
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="validate the account, quote, state, and lock path without broker writes",
    )
    parser.add_argument(
        "--confirm-simulated-action",
        action="store_true",
        help=(
            "acknowledge autonomous simulated-account orders and mandatory "
            "pre-close flattening"
        ),
    )


def _live_record(parser):
    parser.add_argument("--log", required=True)
    parser.add_argument("--day", required=True)
    parser.add_argument("--errors", type=int, required=True)
    parser.add_argument("--drawdown", type=float, required=True)
    parser.add_argument("--notes", default="")


def _live_rollup(parser):
    parser.add_argument("--snapshot-log", required=True)
    parser.add_argument("--runs-log", required=True)
    parser.add_argument("--baseline-equity", type=float)
    parser.add_argument("--min-snapshots-per-day", type=int, default=1)


def _live_evidence(parser):
    parser.add_argument("--log", required=True)
    parser.add_argument("--evidence-file", required=True)
    parser.add_argument("--min-sessions", type=int, default=20)
    parser.add_argument("--max-drawdown", type=float, default=0.15)


def _live_status(parser):
    parser.add_argument("--log", required=True)
    parser.add_argument("--min-sessions", type=int, default=20)
    parser.add_argument("--max-drawdown", type=float, default=0.15)


def _acceptance_cycle(parser):
    parser.add_argument("--evidence-file", required=True)
    parser.add_argument("--paper-snapshot-log", default="data/paper_snapshots.jsonl")
    parser.add_argument("--paper-runs-log", default="data/paper_runs.jsonl")
    parser.add_argument("--paper-backtest-returns-json")
    parser.add_argument("--paper-equity", type=float)
    parser.add_argument("--paper-timestamp")
    parser.add_argument("--paper-alpaca-endpoint")
    parser.add_argument("--skip-paper-snapshot", action="store_true")
    parser.add_argument("--paper-min-snapshots-per-period", type=int, default=2)
    parser.add_argument("--min-paper-weeks", type=int, default=4)
    parser.add_argument("--max-paper-performance-deviation", type=float, default=0.30)
    parser.add_argument("--live-snapshot-log", default="data/live_snapshots.jsonl")
    parser.add_argument("--live-runs-log", default="data/live_runs.jsonl")
    parser.add_argument("--live-equity", type=float)
    parser.add_argument("--live-timestamp")
    parser.add_argument("--live-baseline-equity", type=float)
    parser.add_argument("--live-alpaca-endpoint")
    parser.add_argument("--capture-live-snapshot", action="store_true")
    parser.add_argument("--live-min-snapshots-per-day", type=int, default=1)
    parser.add_argument("--min-live-sessions", type=int, default=20)
    parser.add_argument("--max-live-drawdown", type=float, default=0.15)


COMMAND_SPECS = (
    CommandSpec("preflight", handle_preflight, _preflight),
    CommandSpec("strategy-readiness", handle_strategy_readiness, _strategy_readiness),
    CommandSpec("schedule", handle_schedule, _noop),
    CommandSpec("acceptance-schedule", handle_acceptance_schedule, _acceptance_schedule),
    CommandSpec("paper-acceptance", handle_paper_acceptance, _paper_acceptance),
    CommandSpec("paper-run-record", handle_paper_run_record, _paper_run_record),
    CommandSpec("paper-snapshot-record", handle_paper_snapshot_record, _account_snapshot_record),
    CommandSpec("paper-rollup-snapshots", handle_paper_rollup_snapshots, _paper_rollup),
    CommandSpec("paper-evidence", handle_paper_evidence, _paper_evidence),
    CommandSpec("paper-status", handle_paper_status, _paper_status),
    CommandSpec("live-smoke", handle_live_smoke, _live_smoke),
    CommandSpec("completion-audit", handle_completion_audit, _completion_audit),
    CommandSpec(
        "experiment-acceptance-report",
        handle_experiment_acceptance_report,
        _experiment_acceptance_report,
    ),
    CommandSpec("collect-evidence", handle_collect_evidence, _collect_evidence),
    CommandSpec("runtime-evidence", handle_runtime_evidence, _evidence_file),
    CommandSpec("runtime-smoke", handle_runtime_smoke, _evidence_file),
    CommandSpec("backtest-acceptance", handle_backtest_acceptance, _backtest_acceptance),
    CommandSpec("oos-backtest-run", handle_oos_backtest_run, _oos_backtest),
    CommandSpec("historical-replay", handle_historical_replay, _historical_replay),
    CommandSpec("normal-experiment-run", handle_normal_experiment_run, _normal_experiment),
    CommandSpec("daytrade-schedule", handle_daytrade_schedule, _daytrade_schedule),
    CommandSpec("daytrade-cache-fill", handle_daytrade_cache_fill, _daytrade_cache),
    CommandSpec("daytrade-backtest-run", handle_daytrade_backtest_run, _daytrade_backtest),
    CommandSpec("daytrade-optimize-run", handle_daytrade_optimize_run, _daytrade_optimize),
    CommandSpec("daytrade-compare-configs", handle_daytrade_compare_configs, _daytrade_compare),
    CommandSpec("daytrade-paper-cycle", handle_daytrade_paper_cycle, _daytrade_paper),
    CommandSpec("daytrade-live-cycle", handle_daytrade_live_cycle, _daytrade_live),
    CommandSpec("daytrade-status", handle_daytrade_status, _daytrade_status),
    CommandSpec("moomoo-simulate", handle_moomoo_simulate, _moomoo_simulate),
    CommandSpec(
        "moomoo-paper-canary",
        handle_moomoo_paper_canary,
        _moomoo_paper_canary,
    ),
    CommandSpec("live-soak-record", handle_live_soak_record, _live_record),
    CommandSpec("live-soak-snapshot-record", handle_live_soak_snapshot_record, _account_snapshot_record),
    CommandSpec("live-soak-rollup-snapshots", handle_live_soak_rollup_snapshots, _live_rollup),
    CommandSpec("live-soak-evidence", handle_live_soak_evidence, _live_evidence),
    CommandSpec("live-soak-status", handle_live_soak_status, _live_status),
    CommandSpec("acceptance-cycle", handle_acceptance_cycle, _acceptance_cycle),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quantbot")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for spec in COMMAND_SPECS:
        command_parser = subparsers.add_parser(spec.name)
        spec.configure(command_parser)
        command_parser.set_defaults(_handler=spec.handler)
    return parser

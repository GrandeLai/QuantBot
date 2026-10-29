"""Shared, side-effect-light helpers for CLI handlers."""

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

from ..acceptance_policy import CURRENT_ACCEPTANCE_POLICY
from ..acceptance_validation import valid_error_total
from ..analytics import PerformanceSummary
from ..evidence import EvidenceRecorder, hash_payload
from ..live import evaluate_live_trading_runs
from ..oos_backtest import (
    MomentumOOSBacktestConfig,
    load_alpaca_adjusted_close_prices,
    load_yfinance_adjusted_close_prices,
)
from ..config_loader import load_runtime_config
from ..paper import PaperTradingAcceptance


def resolve_runtime_config(args, environ=None):
    """Load the runtime TOML named by --config or STOCKBOT_CONFIG.

    ``load_runtime_config`` had no caller anywhere in the package: the CLI
    never read a configuration file, so every declared key was inert no matter
    how carefully it was validated and hashed.  Resolution stays explicit and
    optional -- no path, no config -- so commands that do not need one are
    unaffected.
    """

    env = environ if environ is not None else os.environ
    path = getattr(args, "config", "") or env.get("STOCKBOT_CONFIG", "")
    if not path:
        return None
    return load_runtime_config(Path(path))


def resolve_alpaca_feed(args, environ=None, *, config=None, minute=False):
    """Resolve the Alpaca data feed: flag, then environment, then config.

    ``data.daily_feed`` and ``data.minute_feed`` were declared and hashed but
    never consulted.  They now sit at the end of the chain, so an explicit
    flag or environment override still wins.
    """

    env = environ if environ is not None else os.environ
    feed = getattr(args, "alpaca_feed", "") or env.get("ALPACA_DATA_FEED", "")
    if feed:
        return feed
    if config is None:
        return ""
    return config.data.minute_feed if minute else config.data.daily_feed


def format_preflight(payload) -> str:
    if payload["ready"]:
        return "ready"
    return "missing_env=%s missing_packages=%s" % (
        ",".join(payload["missing_env"]),
        ",".join(payload["missing_packages"]),
    )


def paper_runs_hash(runs) -> str:
    return hash_payload(
        [
            {
                "period": run.period,
                "errors": run.errors,
                "paper_return": run.paper_return,
                "backtest_return": run.backtest_return,
            }
            for run in runs
        ]
    )


def live_runs_hash(runs) -> str:
    return hash_payload(
        [
            {
                "day": run.day,
                "errors": run.errors,
                "drawdown": run.drawdown,
            }
            for run in runs
        ]
    )


def journal_provenance(runs, expected_run_type: str, evidence_config: dict) -> dict:
    run_types = {run.run_type for run in runs if getattr(run, "run_type", None)}
    if "historical_replay" in run_types:
        run_type = "historical_replay"
    elif run_types == {expected_run_type}:
        run_type = expected_run_type
    elif len(run_types) == 1:
        run_type = next(iter(run_types))
    else:
        run_type = "unspecified"

    observed_values = [getattr(run, "observed_at", None) for run in runs]
    observed_at = (
        max(observed_values)
        if observed_values and all(observed_values)
        else "unavailable"
    )
    journal_config_hashes = [getattr(run, "config_hash", None) for run in runs]
    code_hashes = [getattr(run, "code_hash", None) for run in runs]
    config_hash = (
        hash_payload(
            {
                "journal_config_hashes": sorted(set(journal_config_hashes)),
                "evidence_config": evidence_config,
            }
        )
        if journal_config_hashes and all(journal_config_hashes)
        else "unavailable"
    )
    code_hash = (
        next(iter(set(code_hashes)))
        if code_hashes and all(code_hashes) and len(set(code_hashes)) == 1
        else hash_payload(sorted(set(code_hashes)))
        if code_hashes and all(code_hashes)
        else "unavailable"
    )
    dataset_hash = (
        paper_runs_hash(runs)
        if expected_run_type == "paper_trading"
        else live_runs_hash(runs)
    )
    return {
        "run_type": run_type,
        "observed_at": observed_at,
        "dataset_hash": dataset_hash,
        "config_hash": config_hash,
        "code_hash": code_hash,
    }


def append_evidence(
    path,
    requirement_id: str,
    passed: bool,
    detail: str,
    **provenance,
) -> None:
    EvidenceRecorder(path).record(requirement_id, passed, detail, **provenance)


def default_import_checker(package_name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(package_name) is not None


def summary_from_json(raw_json: str) -> PerformanceSummary:
    item = json.loads(raw_json)
    return PerformanceSummary(
        annual_return=float(item.get("annual_return", 0.0)),
        benchmark_annual_return=float(item.get("benchmark_annual_return", 0.0)),
        excess_alpha_annual=float(item["excess_alpha_annual"]),
        annual_volatility=float(item.get("annual_volatility", 0.0)),
        sharpe=float(item["sharpe"]),
        max_drawdown=float(item["max_drawdown"]),
    )


def summary_to_payload(summary: PerformanceSummary) -> dict:
    return {
        "annual_return": summary.annual_return,
        "benchmark_annual_return": summary.benchmark_annual_return,
        "excess_alpha_annual": summary.excess_alpha_annual,
        "annual_volatility": summary.annual_volatility,
        "sharpe": summary.sharpe,
        "max_drawdown": summary.max_drawdown,
    }


def oos_backtest_config_payload(
    config: MomentumOOSBacktestConfig,
    args,
    source_name: str,
) -> dict:
    return {
        "symbols": list(config.symbols),
        "benchmark": config.benchmark,
        "train_start": config.train_start.isoformat(),
        "train_end": config.train_end.isoformat(),
        "validation_start": config.validation_start.isoformat(),
        "validation_end": config.validation_end.isoformat(),
        "lookback_days": config.lookback_days,
        "top_n": config.top_n,
        "two_way_cost_rate": config.two_way_cost_rate,
        "data_start": parse_date(args.data_start).isoformat(),
        "data_end": parse_date(args.data_end).isoformat(),
        "source": {
            "requested": args.data_source,
            "resolved": source_name,
            "alpaca_data_url": args.alpaca_data_url or "",
            "alpaca_feed": args.alpaca_feed,
        },
    }


def paper_status_payload(
    runs,
    min_weeks: int,
    max_performance_deviation: float,
    enforce_policy: bool = True,
) -> dict:
    policy_version = None
    if enforce_policy:
        policy = CURRENT_ACCEPTANCE_POLICY.effective(
            min_paper_weeks=min_weeks,
            max_paper_performance_deviation=max_performance_deviation,
        )
        min_weeks = policy.min_paper_weeks
        max_performance_deviation = policy.max_paper_performance_deviation
        policy_version = policy.policy_version
    decision = PaperTradingAcceptance(
        min_weeks=min_weeks,
        max_performance_deviation=max_performance_deviation,
    ).evaluate(runs)
    return {
        "passed": decision.passed,
        "weeks": decision.weeks,
        "min_weeks": min_weeks,
        "remaining_weeks": max(0, min_weeks - decision.weeks),
        "errors": valid_error_total(run.errors for run in runs),
        "performance_deviation": decision.performance_deviation,
        "max_performance_deviation": max_performance_deviation,
        "policy_version": policy_version,
        "reasons": decision.reasons,
    }


def live_status_payload(
    runs,
    min_sessions: int,
    max_drawdown: float,
    enforce_policy: bool = True,
) -> dict:
    policy_version = None
    if enforce_policy:
        policy = CURRENT_ACCEPTANCE_POLICY.effective(
            min_live_sessions=min_sessions,
            max_live_drawdown=max_drawdown,
        )
        min_sessions = policy.min_live_sessions
        max_drawdown = policy.max_live_drawdown
        policy_version = policy.policy_version
    decision = evaluate_live_trading_runs(
        runs,
        min_sessions=min_sessions,
        max_drawdown=max_drawdown,
    )
    return {
        "passed": decision.passed,
        "sessions": decision.sessions,
        "min_sessions": min_sessions,
        "remaining_sessions": max(0, min_sessions - decision.sessions),
        "errors": decision.errors,
        "worst_drawdown": decision.worst_drawdown,
        "max_drawdown": max_drawdown,
        "policy_version": policy_version,
        "reasons": decision.reasons,
    }


def write_record_error(stdout, error: ValueError) -> int:
    stdout.write(
        json.dumps({"recorded": False, "error": str(error)}, sort_keys=True) + "\n"
    )
    return 1


def parse_symbols(raw_symbols: str):
    symbols = tuple(
        symbol.strip().upper()
        for symbol in raw_symbols.split(",")
        if symbol.strip()
    )
    if not symbols:
        raise ValueError("at least one symbol is required")
    return symbols


def parse_date(raw_date: str) -> date:
    return date.fromisoformat(raw_date)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_paper_account_value_loader(args, environ):
    from ..integrations.alpaca import AlpacaTradingClient

    env = environ or os.environ
    api_key = env.get("ALPACA_API_KEY")
    secret_key = env.get("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        raise ValueError(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY are required for "
            "paper snapshot capture"
        )
    endpoint = (
        getattr(args, "alpaca_endpoint", None)
        or getattr(args, "paper_alpaca_endpoint", None)
        or env.get("ALPACA_ENDPOINT")
        or "https://paper-api.alpaca.markets"
    )
    client = AlpacaTradingClient(
        api_key=api_key,
        secret_key=secret_key,
        base_url=endpoint,
    )
    return client.fetch_portfolio_value


def resolve_live_account_value_loader(args, environ):
    from ..integrations.alpaca import AlpacaTradingClient

    env = environ or os.environ
    api_key = env.get("ALPACA_API_KEY")
    secret_key = env.get("ALPACA_SECRET_KEY")
    endpoint = (
        getattr(args, "alpaca_endpoint", None)
        or getattr(args, "live_alpaca_endpoint", None)
        or env.get("ALPACA_LIVE_ENDPOINT")
    )
    if not api_key or not secret_key:
        raise ValueError(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY are required for "
            "live soak snapshot capture"
        )
    if not endpoint:
        raise ValueError(
            "ALPACA_LIVE_ENDPOINT or --alpaca-endpoint is required for "
            "live soak snapshot capture"
        )
    client = AlpacaTradingClient(
        api_key=api_key,
        secret_key=secret_key,
        base_url=endpoint,
    )
    return client.fetch_portfolio_value


def resolve_oos_price_loader(args, environ, injected_loader):
    if injected_loader:
        return injected_loader, "injected"

    env = environ or os.environ
    data_source = args.data_source
    if data_source == "auto":
        data_source = (
            "alpaca"
            if env.get("ALPACA_API_KEY") and env.get("ALPACA_SECRET_KEY")
            else "yfinance"
        )

    if data_source == "alpaca":
        api_key = env.get("ALPACA_API_KEY")
        secret_key = env.get("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            raise ValueError(
                "ALPACA_API_KEY and ALPACA_SECRET_KEY are required for "
                "Alpaca OOS data"
            )
        base_url = alpaca_bars_url(
            args.alpaca_data_url
            or env.get("ALPACA_DATA_ENDPOINT")
            or "https://data.alpaca.markets"
        )
        feed = resolve_alpaca_feed(args, env, config=resolve_runtime_config(args, env))

        def load_from_alpaca(symbols, benchmark, start, end):
            return load_alpaca_adjusted_close_prices(
                symbols=symbols,
                benchmark=benchmark,
                start=start,
                end=end,
                api_key=api_key,
                secret_key=secret_key,
                base_url=base_url,
                feed=feed,
            )

        suffix = ":%s" % feed if feed else ""
        return load_from_alpaca, "alpaca_adjusted_close%s" % suffix

    return load_yfinance_adjusted_close_prices, "yfinance_adjusted_close"


def alpaca_bars_url(endpoint: str) -> str:
    base_url = endpoint.rstrip("/")
    if base_url.endswith("/v2/stocks/bars"):
        return base_url
    return base_url + "/v2/stocks/bars"


def default_runtime_smoke_suite_factory():
    from ..runtime_factory import RuntimeSmokeFactory

    return RuntimeSmokeFactory().build()

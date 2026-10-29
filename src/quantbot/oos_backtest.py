"""Executable OOS backtest workflow for the Phase 5 acceptance gate."""

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from typing import Dict, Iterable, Mapping, Sequence, Tuple

from .analytics import PerformanceSummary, performance_summary
from .backtest import OOSBacktestWindowReport, evaluate_oos_backtest_window, turnover_rate


DEFAULT_OOS_SYMBOLS = (
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "META",
    "GOOGL",
    "JPM",
    "UNH",
    "XOM",
    "JNJ",
    "PG",
    "HD",
    "AVGO",
    "COST",
    "LLY",
    "V",
    "MA",
    "NFLX",
    "CRM",
    "AMD",
)
DEFAULT_BENCHMARK = "SPY"


PriceSeries = Mapping[str, Mapping[date, float]]
ExecutionReturnSeries = Mapping[str, Mapping[date, float]]


@dataclass(frozen=True)
class DailyReturnPoint:
    trading_date: date
    portfolio_return: float
    benchmark_return: float


@dataclass(frozen=True)
class MomentumOOSBacktestConfig:
    symbols: Tuple[str, ...] = DEFAULT_OOS_SYMBOLS
    benchmark: str = DEFAULT_BENCHMARK
    train_start: date = date(2015, 1, 1)
    train_end: date = date(2022, 12, 31)
    validation_start: date = date(2023, 1, 1)
    validation_end: date = date(2024, 12, 31)
    lookback_days: int = 252
    top_n: int = 5
    two_way_cost_rate: float = 0.0


@dataclass(frozen=True)
class MomentumOOSBacktestResult:
    summary: PerformanceSummary
    train_summary: PerformanceSummary
    annual_turnover: float
    train_annual_turnover: float
    window_report: OOSBacktestWindowReport
    coverage_reasons: Tuple[str, ...]
    validation_observations: int
    train_observations: int
    validation_rebalances: int
    train_rebalances: int
    symbols: Tuple[str, ...]
    benchmark: str
    validation_daily_returns: Tuple[DailyReturnPoint, ...]
    train_daily_returns: Tuple[DailyReturnPoint, ...]


@dataclass(frozen=True)
class _PeriodResult:
    daily_points: Tuple[DailyReturnPoint, ...]
    daily_returns: Tuple[float, ...]
    benchmark_returns: Tuple[float, ...]
    annual_turnover: float
    rebalance_count: int


LEGACY_UNIVERSE_MODE = "static_mega_cap"


def require_legacy_universe_mode(universe_mode: str) -> None:
    """Refuse the legacy static universe when configuration demands PIT.

    ``normal.universe_mode`` was validated and hashed but read by nothing, so a
    configuration declaring ``point_in_time`` could still be served by this
    fixed mega-cap list, which has survivorship bias and no membership history.
    """

    if str(universe_mode) != LEGACY_UNIVERSE_MODE:
        raise ValueError(
            "normal.universe_mode=%r forbids the legacy static mega-cap "
            "universe; use the point-in-time normal experiment path"
            % (universe_mode,)
        )


def run_momentum_oos_backtest(
    prices: PriceSeries,
    config: MomentumOOSBacktestConfig = MomentumOOSBacktestConfig(),
    execution_returns: ExecutionReturnSeries = None,
    overnight_returns: ExecutionReturnSeries = None,
) -> MomentumOOSBacktestResult:
    """Run a fixed-parameter monthly momentum validation without lookahead.

    Rebalance decisions use prices available at the previous trading day's close.
    The first out-of-sample portfolio return is therefore generated after a
    signal date strictly earlier than the return date.
    """

    if config.top_n < 1:
        raise ValueError("top_n must be positive")
    if config.lookback_days < 1:
        raise ValueError("lookback_days must be positive")
    if config.two_way_cost_rate < 0:
        raise ValueError("two_way_cost_rate must be non-negative")

    if (execution_returns is None) != (overnight_returns is None):
        raise ValueError("execution_returns and overnight_returns must be provided together")

    clean_prices = _normalize_prices(prices)
    clean_execution_returns = _normalize_returns(execution_returns or {})
    clean_overnight_returns = _normalize_returns(overnight_returns or {})
    missing = [ticker for ticker in (config.symbols + (config.benchmark,)) if ticker not in clean_prices]
    if missing:
        raise ValueError("missing price series: %s" % ",".join(sorted(set(missing))))

    train = _run_period(
        clean_prices,
        symbols=config.symbols,
        benchmark=config.benchmark,
        start=config.train_start,
        end=config.train_end,
        lookback_days=config.lookback_days,
        top_n=config.top_n,
        two_way_cost_rate=config.two_way_cost_rate,
        execution_returns=clean_execution_returns if execution_returns is not None else None,
        overnight_returns=clean_overnight_returns if overnight_returns is not None else None,
    )
    validation = _run_period(
        clean_prices,
        symbols=config.symbols,
        benchmark=config.benchmark,
        start=config.validation_start,
        end=config.validation_end,
        lookback_days=config.lookback_days,
        top_n=config.top_n,
        two_way_cost_rate=config.two_way_cost_rate,
        execution_returns=clean_execution_returns if execution_returns is not None else None,
        overnight_returns=clean_overnight_returns if overnight_returns is not None else None,
    )

    return MomentumOOSBacktestResult(
        summary=performance_summary(validation.daily_returns, validation.benchmark_returns),
        train_summary=performance_summary(train.daily_returns, train.benchmark_returns),
        annual_turnover=validation.annual_turnover,
        train_annual_turnover=train.annual_turnover,
        window_report=evaluate_oos_backtest_window(
            train_start=config.train_start,
            train_end=config.train_end,
            validation_start=config.validation_start,
            validation_end=config.validation_end,
        ),
        coverage_reasons=_coverage_reasons(clean_prices, config),
        validation_observations=len(validation.daily_returns),
        train_observations=len(train.daily_returns),
        validation_rebalances=validation.rebalance_count,
        train_rebalances=train.rebalance_count,
        symbols=config.symbols,
        benchmark=config.benchmark,
        validation_daily_returns=validation.daily_points,
        train_daily_returns=train.daily_points,
    )


def load_yfinance_adjusted_close_prices(
    symbols: Iterable[str],
    benchmark: str,
    start: date,
    end: date,
    client=None,
) -> Dict[str, Dict[date, float]]:
    """Fetch adjusted closes through the local Yahoo Finance integration."""

    if client is None:
        try:
            prices = _load_yfinance_package_adjusted_close_prices(symbols, benchmark, start, end)
            if all(prices[ticker] for ticker in _unique_symbols(tuple(symbols) + (benchmark,))):
                return prices
        except Exception:
            pass

    from .integrations.yfinance import YFinanceClient

    client = client or YFinanceClient()
    tickers = _unique_symbols(tuple(symbols) + (benchmark,))
    prices: Dict[str, Dict[date, float]] = {}
    for ticker in tickers:
        rows = client.fetch_daily_bars(ticker, start=start, end=end)
        series: Dict[date, float] = {}
        for row in rows:
            value = row.get("adjusted_close")
            if value is None:
                value = row.get("close")
            if value is None:
                continue
            price = float(value)
            if price > 0:
                series[row["date"]] = price
        prices[ticker] = series
    return prices


def _load_yfinance_package_adjusted_close_prices(
    symbols: Iterable[str],
    benchmark: str,
    start: date,
    end: date,
) -> Dict[str, Dict[date, float]]:
    import yfinance as yf

    tickers = _unique_symbols(tuple(symbols) + (benchmark,))
    frame = yf.download(
        list(tickers),
        start=start.isoformat(),
        end=end.isoformat(),
        auto_adjust=True,
        progress=False,
        threads=True,
    )
    if frame is None or frame.empty:
        return {ticker: {} for ticker in tickers}

    close = _close_frame(frame)
    prices: Dict[str, Dict[date, float]] = {ticker: {} for ticker in tickers}
    for ticker in tickers:
        if hasattr(close, "columns"):
            if ticker not in close.columns:
                continue
            series = close[ticker]
        else:
            if len(tickers) != 1:
                continue
            series = close
        for timestamp, value in series.dropna().items():
            price = float(value)
            if price > 0:
                prices[ticker][timestamp.date()] = price
    return prices


def _close_frame(frame):
    columns = frame.columns
    if getattr(columns, "nlevels", 1) > 1:
        level0 = columns.get_level_values(0)
        if "Close" in level0:
            return frame["Close"]
        if "Adj Close" in level0:
            return frame["Adj Close"]
    if "Close" in frame:
        return frame["Close"]
    return frame["Adj Close"]


def load_alpaca_adjusted_close_prices(
    symbols: Iterable[str],
    benchmark: str,
    start: date,
    end: date,
    api_key: str,
    secret_key: str,
    base_url: str = "https://data.alpaca.markets/v2/stocks/bars",
    feed: str = "",
) -> Dict[str, Dict[date, float]]:
    """Fetch adjusted closes through Alpaca's stock bars endpoint."""

    from .integrations.alpaca import AlpacaDataClient

    tickers = _unique_symbols(tuple(symbols) + (benchmark,))
    client = AlpacaDataClient(
        api_key=api_key,
        secret_key=secret_key,
        base_url=base_url,
        feed=feed,
    )
    prices = {ticker: {} for ticker in tickers}
    for row in client.fetch_daily_bars(tickers, start=start, end=end):
        value = row.get("adjusted_close")
        if value is None:
            value = row.get("close")
        if value is None:
            continue
        price = float(value)
        if price > 0:
            prices[row["ticker"].upper()][row["date"]] = price
    return prices


def _run_period(
    prices: Dict[str, Dict[date, float]],
    symbols: Tuple[str, ...],
    benchmark: str,
    start: date,
    end: date,
    lookback_days: int,
    top_n: int,
    two_way_cost_rate: float,
    execution_returns: Dict[str, Dict[date, float]] = None,
    overnight_returns: Dict[str, Dict[date, float]] = None,
) -> _PeriodResult:
    benchmark_dates = sorted(prices[benchmark])
    benchmark_index = {trading_date: index for index, trading_date in enumerate(benchmark_dates)}
    period_dates = [trading_date for trading_date in benchmark_dates if start <= trading_date <= end]
    symbol_dates = {ticker: sorted(prices[ticker]) for ticker in symbols}

    weights: Dict[str, float] = {}
    current_rebalance_month = None
    turnover_sum = 0.0
    rebalance_count = 0
    daily_returns = []
    benchmark_returns = []
    daily_points = []

    for current_date in period_dates:
        index = benchmark_index[current_date]
        if index == 0:
            continue
        previous_date = benchmark_dates[index - 1]
        month_key = (current_date.year, current_date.month)
        next_weights = None
        if month_key != current_rebalance_month:
            next_weights = _select_momentum_weights(
                prices,
                symbol_dates,
                symbols=symbols,
                signal_date=previous_date,
                lookback_days=lookback_days,
                top_n=top_n,
            )
            current_rebalance_month = month_key
            rebalance_count += 1

        turnover = 0.0
        if next_weights is not None and execution_returns is not None:
            missing_overnight = sorted(
                ticker
                for ticker in weights
                if current_date not in overnight_returns.get(ticker, {})
            )
            if missing_overnight:
                raise ValueError(
                    "missing overnight returns for rebalance date %s: %s"
                    % (current_date.isoformat(), ",".join(missing_overnight))
                )
            overnight_asset_returns = {
                ticker: overnight_returns[ticker][current_date]
                for ticker in weights
            }
            overnight_portfolio_return = sum(
                weight * overnight_asset_returns[ticker]
                for ticker, weight in weights.items()
            )
            weights = _drift_weights(
                weights,
                overnight_asset_returns,
                overnight_portfolio_return,
            )
            turnover = turnover_rate(weights, next_weights)
            turnover_sum += turnover
            weights = next_weights
            missing_execution = sorted(
                ticker
                for ticker in weights
                if current_date not in execution_returns.get(ticker, {})
            )
            if missing_execution:
                raise ValueError(
                    "missing execution returns for rebalance date %s: %s"
                    % (current_date.isoformat(), ",".join(missing_execution))
                )
            asset_returns = {
                ticker: execution_returns[ticker][current_date]
                for ticker in weights
            }
            intraday_portfolio_return = sum(
                weight * asset_returns[ticker]
                for ticker, weight in weights.items()
            )
            weights = _drift_weights(weights, asset_returns, intraday_portfolio_return)
            gross_portfolio_return = (
                (1.0 + overnight_portfolio_return)
                * (1.0 + intraday_portfolio_return)
                - 1.0
            )
        else:
            asset_returns = {
                ticker: _close_to_close_return(prices[ticker], previous_date, current_date)
                for ticker in weights
            }
            gross_portfolio_return = sum(
                weight * asset_returns[ticker]
                for ticker, weight in weights.items()
            )
            weights = _drift_weights(weights, asset_returns, gross_portfolio_return)
            if next_weights is not None:
                turnover = turnover_rate(weights, next_weights)
                turnover_sum += turnover
                weights = next_weights
        transaction_cost = turnover * two_way_cost_rate
        portfolio_return = (1.0 + gross_portfolio_return) * (1.0 - transaction_cost) - 1.0
        benchmark_return = _close_to_close_return(prices[benchmark], previous_date, current_date)
        daily_returns.append(portfolio_return)
        benchmark_returns.append(benchmark_return)
        daily_points.append(
            DailyReturnPoint(
                trading_date=current_date,
                portfolio_return=portfolio_return,
                benchmark_return=benchmark_return,
            )
        )

    years = len(daily_returns) / 252 if daily_returns else 0.0
    annual_turnover = turnover_sum / years if years else 0.0
    return _PeriodResult(
        daily_points=tuple(daily_points),
        daily_returns=tuple(daily_returns),
        benchmark_returns=tuple(benchmark_returns),
        annual_turnover=annual_turnover,
        rebalance_count=rebalance_count,
    )


def _drift_weights(
    weights: Mapping[str, float],
    asset_returns: Mapping[str, float],
    portfolio_return: float,
) -> Dict[str, float]:
    growth = 1.0 + float(portfolio_return)
    if growth <= 0:
        return {}
    return {
        ticker: float(weight) * (1.0 + float(asset_returns.get(ticker, 0.0))) / growth
        for ticker, weight in weights.items()
        if float(weight) != 0.0
    }


def _coverage_reasons(
    prices: Dict[str, Dict[date, float]],
    config: MomentumOOSBacktestConfig,
) -> Tuple[str, ...]:
    reasons = []
    for ticker in config.symbols + (config.benchmark,):
        dates = sorted(prices[ticker])
        if not any(trading_date <= config.train_start for trading_date in dates):
            reasons.append("data_start_after_train:%s" % ticker)
        if not any(trading_date >= config.validation_end for trading_date in dates):
            reasons.append("data_end_before_validation:%s" % ticker)
    return tuple(reasons)


def _select_momentum_weights(
    prices: Dict[str, Dict[date, float]],
    symbol_dates: Mapping[str, Sequence[date]],
    symbols: Tuple[str, ...],
    signal_date: date,
    lookback_days: int,
    top_n: int,
) -> Dict[str, float]:
    ranked = []
    for ticker in symbols:
        dates = symbol_dates[ticker]
        signal_index = bisect_right(dates, signal_date) - 1
        if signal_index < lookback_days:
            continue
        current_price = prices[ticker][dates[signal_index]]
        lookback_price = prices[ticker][dates[signal_index - lookback_days]]
        if lookback_price <= 0:
            continue
        ranked.append((current_price / lookback_price - 1.0, ticker))

    selected = [ticker for _score, ticker in sorted(ranked, key=lambda item: (-item[0], item[1]))[:top_n]]
    if not selected:
        return {}
    weight = 1.0 / len(selected)
    return {ticker: weight for ticker in selected}


def _close_to_close_return(series: Mapping[date, float], previous_date: date, current_date: date) -> float:
    previous_price = series.get(previous_date)
    current_price = series.get(current_date)
    if previous_price is None or current_price is None or previous_price <= 0:
        return 0.0
    return current_price / previous_price - 1.0


def _normalize_prices(prices: PriceSeries) -> Dict[str, Dict[date, float]]:
    clean: Dict[str, Dict[date, float]] = {}
    for ticker, series in prices.items():
        normalized_ticker = ticker.upper()
        clean[normalized_ticker] = {
            trading_date: float(value)
            for trading_date, value in series.items()
            if value is not None and float(value) > 0
        }
    return clean


def normalized_prices_payload(prices: PriceSeries) -> dict:
    """Return the canonical JSON-compatible price input used for provenance."""

    clean = _normalize_prices(prices)
    return {
        ticker: {
            trading_date.isoformat(): clean[ticker][trading_date]
            for trading_date in sorted(clean[ticker])
        }
        for ticker in sorted(clean)
    }


def _normalize_returns(returns: ExecutionReturnSeries) -> Dict[str, Dict[date, float]]:
    return {
        ticker.upper(): {
            trading_date: float(value)
            for trading_date, value in series.items()
            if value is not None
        }
        for ticker, series in returns.items()
    }


def _unique_symbols(symbols: Sequence[str]) -> Tuple[str, ...]:
    seen = set()
    output = []
    for symbol in symbols:
        normalized = symbol.strip().upper()
        if normalized and normalized not in seen:
            seen.add(normalized)
            output.append(normalized)
    return tuple(output)

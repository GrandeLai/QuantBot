"""Versioned, causal daily crypto factors and separate forward event labels.

CSV timestamps denote UTC candle opens. Features become available at the next
midnight; fixed-horizon labels enter at that midnight and exit h opens later.
Labels describe events, not a protected portfolio or realized execution.
"""

from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from quantbot.evidence import hash_payload


SYMBOLS = ("BTC/USDT", "ETH/USDT")
HORIZONS = (1, 5, 10)
DAY = pd.Timedelta(86400, unit="s")


@dataclass(frozen=True)
class FactorDefinition:
    name: str
    family: str
    role: str
    lookback: int
    formula: str
    hypothesis: str
    failure_condition: str
    symbols: tuple[str, ...] = SYMBOLS
    version: str = "1.0.0"
    inputs: tuple[str, ...] = ("open", "high", "low", "close", "volume")
    expected_direction: str = "higher_predicts_higher_return"
    unit: str = "dimensionless"

    def to_dict(self):
        value = asdict(self)
        return {**value, "definition_hash": hash_payload(value)}


FACTORS = (
    FactorDefinition("momentum_20", "trend", "alpha", 21,
                     "(close[t]/close[t-20]-1)/std(daily_return,20,ddof=1)",
                     "Volatility-adjusted strength persists", "Choppy or reversing trend"),
    FactorDefinition("momentum_60", "trend", "alpha", 61,
                     "(close[t]/close[t-60]-1)/std(daily_return,60,ddof=1)",
                     "Medium-term strength persists", "Trend reversal"),
    FactorDefinition("breakout_20", "breakout", "alpha", 21,
                     "(close[t]-max(high[t-20:t]))/mean(true_range,14)",
                     "A close near/above prior highs predicts continuation", "False breakout"),
    FactorDefinition("close_location", "breakout", "alpha", 1,
                     "(2*close-high-low)/(high-low)",
                     "A close near the high signals buying pressure", "Intraday reversal"),
    FactorDefinition("ema_reversal", "reversal", "alpha", 20,
                     "-(close-EMA20(adjust=False,seed=first_close))/mean(true_range,14)",
                     "A negative deviation recovers", "Persistent directional trend"),
    FactorDefinition("reversal_3", "reversal", "alpha", 21,
                     "-(close[t]/close[t-3]-1)/std(daily_return,20,ddof=1)",
                     "Short overshoots reverse", "Persistent directional trend"),
    FactorDefinition("signed_volume", "volume", "alpha", 20,
                     "sign(daily_return)*volume/mean(volume,20)",
                     "Directional volume confirms continuation", "Exhaustion or unreliable volume"),
    FactorDefinition("illiquidity_20", "liquidity", "risk", 21,
                     "mean(abs(daily_return)/(close*base_volume),20)",
                     "High price impact proxies poor liquidity", "Volume is not executable depth",
                     expected_direction="diagnostic_only", unit="inverse_quote_currency"),
    FactorDefinition("efficiency_10", "regime", "state", 11,
                     "abs(close[t]-close[t-10])/sum(abs(diff(close)),10)",
                     "Distinguish directional from choppy periods", "Backward-looking regime shifts",
                     expected_direction="diagnostic_only"),
    FactorDefinition("volatility_ratio", "regime", "state", 21,
                     "std(daily_return,5,ddof=1)/std(daily_return,20,ddof=1)",
                     "Identify volatility expansion", "No direction information",
                     expected_direction="diagnostic_only"),
    FactorDefinition("relative_momentum_20", "relative", "alpha", 21,
                     "log(close_symbol/close_other)[t]-log(close_symbol/close_other)[t-20]",
                     "Relative strength persists; BTC and ETH scores are dependent", "Common risk dominates",
                     inputs=("BTC/USDT.close", "ETH/USDT.close"), unit="log_return"),
    FactorDefinition("eth_residual_20", "relative", "alpha", 81,
                     "sum(ETH_log_return-alpha-beta*BTC_log_return,t-19:t); OLS fit t-79:t-20 (60 returns)",
                     "ETH strength beyond lagged BTC exposure persists", "Unstable beta or shared shocks",
                     symbols=("ETH/USDT",), inputs=("BTC/USDT.close", "ETH/USDT.close"), unit="log_return"),
)


def utc_midnight(value: str) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0 or stamp != stamp.normalize():
        raise ValueError("daily boundaries must be timezone-aware UTC midnights")
    return stamp.tz_convert("UTC")


def load_daily_data(path: Path, development_end: str, protected_holdout_start: str) -> pd.DataFrame:
    """Reject, rather than silently trim, any data outside the frozen boundary."""
    end, holdout = utc_midnight(development_end), utc_midnight(protected_holdout_start)
    if end >= holdout:
        raise ValueError("development_end must precede protected_holdout_start")
    frame = pd.read_csv(path, dtype={"timestamp": str, "symbol": str, "bar": str})
    required = {"timestamp", "symbol", "bar", "open", "high", "low", "close", "volume"}
    if not required.issubset(frame.columns) or frame.empty:
        raise ValueError("daily data requires timestamp,symbol,bar and OHLCV")
    if set(frame.symbol) != set(SYMBOLS) or set(frame.bar) != {"1d"}:
        raise ValueError("factor study v1 requires exactly BTC/USDT and ETH/USDT daily data")
    frame["timestamp"] = [utc_midnight(value) for value in frame.timestamp]
    frame["as_of"] = frame.timestamp + DAY
    if frame.as_of.max() > end:
        raise ValueError("input contains data after development_end; protected data must not be imported")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="raise")
    numbers = frame[["open", "high", "low", "close", "volume"]].to_numpy(dtype=float)
    if not np.isfinite(numbers).all() or (numbers[:, :4] <= 0).any() or (numbers[:, 4] < 0).any():
        raise ValueError("OHLC must be positive finite numbers; volume must be nonnegative and finite")
    if ((frame.low > frame[["open", "close"]].min(axis=1)) |
        (frame.high < frame[["open", "close"]].max(axis=1)) | (frame.low > frame.high)).any():
        raise ValueError("inconsistent OHLC range")
    if frame.duplicated(["symbol", "timestamp"]).any():
        raise ValueError("duplicate symbol timestamp")
    timelines = []
    for symbol in SYMBOLS:
        stamps = frame.loc[frame.symbol == symbol, "timestamp"].sort_values().reset_index(drop=True)
        if not stamps.diff().dropna().eq(DAY).all():
            raise ValueError("daily history has gaps")
        timelines.append(stamps)
    if not timelines[0].equals(timelines[1]):
        raise ValueError("BTC/ETH timelines must match; missing candles cannot be filled")
    return frame.sort_values(["symbol", "timestamp"]).reset_index(drop=True)


def build_panel(data: pd.DataFrame) -> pd.DataFrame:
    """Every formula uses only current or prior bars; labels are built separately."""
    frames = {symbol: data[data.symbol == symbol].reset_index(drop=True) for symbol in SYMBOLS}
    records = []
    for symbol, frame in frames.items():
        close, volume = frame.close, frame.volume
        daily = close.pct_change(fill_method=None)
        std20 = daily.rolling(20).std(ddof=1)
        tr = pd.concat([frame.high - frame.low, (frame.high - close.shift()).abs(),
                        (frame.low - close.shift()).abs()], axis=1).max(axis=1)
        atr14 = tr.rolling(14).mean()
        other = frames[SYMBOLS[1] if symbol == SYMBOLS[0] else SYMBOLS[0]]
        values = {
            "momentum_20": close.pct_change(20, fill_method=None) / std20,
            "momentum_60": close.pct_change(60, fill_method=None) / daily.rolling(60).std(ddof=1),
            "breakout_20": (close - frame.high.shift(1).rolling(20).max()) / atr14,
            "close_location": (2 * close - frame.high - frame.low) / (frame.high - frame.low),
            "ema_reversal": -(close - close.ewm(span=20, adjust=False).mean()) / atr14,
            "reversal_3": -close.pct_change(3, fill_method=None) / std20,
            "signed_volume": np.sign(daily) * volume / volume.rolling(20).mean(),
            "illiquidity_20": (daily.abs() / (close * volume.replace(0, np.nan))).rolling(20).mean(),
            "efficiency_10": close.diff(10).abs() / close.diff().abs().rolling(10).sum(),
            "volatility_ratio": daily.rolling(5).std(ddof=1) / std20,
            "relative_momentum_20": np.log(close / other.close).diff(20),
            "eth_residual_20": pd.Series(np.nan, index=frame.index),
        }
        if symbol == "ETH/USDT":
            btc = np.log(other.close).diff().to_numpy()
            eth = np.log(close).diff().to_numpy()
            for t in range(80, len(frame)):
                x, y = btc[t-79:t-19], eth[t-79:t-19]
                variance = np.mean((x - x.mean()) ** 2)
                if variance <= 1e-20:
                    continue
                beta = np.mean((x - x.mean()) * (y - y.mean())) / variance
                alpha = y.mean() - beta * x.mean()
                values["eth_residual_20"].iat[t] = float(np.sum(eth[t-19:t+1] - alpha - beta * btc[t-19:t+1]))
        for definition in FACTORS:
            for i, row in frame.iterrows():
                value = float(values[definition.name].iat[i])
                reason = None
                if symbol not in definition.symbols:
                    reason = "not_applicable"
                elif i + 1 < definition.lookback:
                    reason = "insufficient_history"
                elif definition.name in {"signed_volume", "illiquidity_20"} and volume.iloc[max(0,i-19):i+1].eq(0).any():
                    reason = "zero_volume"
                elif not np.isfinite(value):
                    reason = "undefined_denominator"
                records.append({"as_of": row.as_of, "symbol": symbol, "factor": definition.name,
                                "version": definition.version, "value": None if reason else value,
                                "missing_reason": reason})
    return pd.DataFrame.from_records(records)


def build_labels(data: pd.DataFrame, one_way_bps: float = 15.0) -> pd.DataFrame:
    if not np.isfinite(one_way_bps) or not 0 <= one_way_bps < 5000:
        raise ValueError("invalid one-way cost")
    cost = one_way_bps / 10000
    records = []
    for symbol in SYMBOLS:
        frame = data[data.symbol == symbol].reset_index(drop=True)
        for i, row in frame.iterrows():
            for horizon in HORIZONS:
                entry, exit_ = i + 1, i + 1 + horizon
                record = {"as_of": row.as_of, "symbol": symbol, "horizon": horizon,
                          "entry_at": row.as_of, "exit_at": row.as_of + horizon * DAY,
                          "gross_return": None, "net_return": None, "stress_return": None,
                          "missing_reason": "missing_future"}
                if exit_ < len(frame):
                    if frame.volume.iat[entry] <= 0 or frame.volume.iat[exit_] <= 0:
                        record["missing_reason"] = "zero_volume_execution_bar"
                    else:
                        ratio = float(frame.open.iat[exit_] / frame.open.iat[entry])
                        record.update(gross_return=ratio - 1, net_return=ratio*(1-cost)/(1+cost)-1,
                                      stress_return=ratio*(1-2*cost)/(1+2*cost)-1, missing_reason=None)
                records.append(record)
    paired = {(row["symbol"], row["as_of"], row["horizon"]): row for row in records}
    for row in records:
        other_symbol = SYMBOLS[1] if row["symbol"] == SYMBOLS[0] else SYMBOLS[0]
        other = paired[(other_symbol, row["as_of"], row["horizon"])]
        row["relative_gross_return"] = (
            row["gross_return"] - other["gross_return"]
            if row["gross_return"] is not None and other["gross_return"] is not None else None
        )
    return pd.DataFrame.from_records(records)

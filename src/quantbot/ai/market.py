"""日线 CSV 到点时特征；复用 PABot 特征与 StockBot 因子评分。"""

from __future__ import annotations

import csv
from datetime import date, datetime, time
import math
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from quantbot.evidence import hash_payload
from quantbot.price_action.priceaction.features import build_features
from quantbot.price_action.strategy.trend_breakout import evaluate_entry_signal
from quantbot.regime import Regime
from quantbot.trading.domain import Bar
from quantbot.trading_agent import TradingAgent, default_regime_weight_config

from .domain import MarketContext


EASTERN = ZoneInfo("America/New_York")
FIELDS = ("date", "symbol", "open", "high", "low", "close", "volume")


def session_time(day: date, *, close=True) -> datetime:
    return datetime.combine(day, time(16) if close else time(9, 30), tzinfo=EASTERN)


class DailyMarketData:
    def __init__(self, rows: list[dict], config: dict):
        self.config = config
        self.frames = {}
        symbols = config["market"]["symbols"]
        selected = {symbol: [] for symbol in symbols}
        seen = set()
        for row in rows:
            if row["symbol"] not in selected:
                continue
            day = date.fromisoformat(str(row["date"]))
            symbol = row["symbol"]
            if (day, symbol) in seen:
                raise ValueError(f"重复日线：{symbol} {day}")
            seen.add((day, symbol))
            values = {key: float(row[key]) for key in FIELDS[2:]}
            bar = Bar(symbol, session_time(day), **values)
            selected[symbol].append(
                {
                    "date": day,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                }
            )
        for symbol, records in selected.items():
            if not records:
                raise ValueError(f"缺少 {symbol} 的日线")
            frame = pd.DataFrame(records).sort_values("date").set_index("date")
            frame.index = pd.DatetimeIndex(frame.index)
            self.frames[symbol] = frame
        self._features = {}

    @classmethod
    def from_csv(cls, path: str | Path, config: dict):
        with Path(path).open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or set(reader.fieldnames) != set(FIELDS):
                raise ValueError(f"CSV 必须包含且仅包含 {','.join(FIELDS)}")
            return cls(list(reader), config)

    @property
    def common_dates(self) -> list[date]:
        return sorted(set.intersection(*(set(frame.index.date) for frame in self.frames.values())))

    def bars(self, day: date, *, close=False) -> dict[str, Bar]:
        result = {}
        for symbol, frame in self.frames.items():
            row = frame.loc[pd.Timestamp(day)]
            result[symbol] = Bar(
                symbol, session_time(day, close=close), **{key: float(row[key]) for key in FIELDS[2:]}
            )
        return result

    def context(self, day: date) -> MarketContext:
        cfg = self.config["strategy"]
        key = day.isoformat()
        if key in self._features:
            return self._features[key]
        rows, previous_rows, prefix = {}, {}, {}
        for symbol, frame in self.frames.items():
            history = frame.loc[frame.index <= pd.Timestamp(day)]
            if history.empty or history.index[-1].date() != day:
                raise ValueError(f"{symbol} 在信号日 {day} 缺少完整日线")
            minimum = max(cfg["momentum_window"] + 1, cfg["atr_window"], cfg["breakout_window"] + 1, 64)
            if len(history) < minimum:
                raise ValueError(f"{symbol} 历史不足：需要至少 {minimum} 根日线")
            built = build_features(history, cfg)
            rows[symbol], previous_rows[symbol] = built.iloc[-1], built.iloc[-2]
            prefix[symbol] = [
                {"date": index.date().isoformat(), **{field: float(value[field]) for field in FIELDS[2:]}}
                for index, value in history.iterrows()
            ]
        bench = rows[self.config["market"]["benchmark"]]
        momentum, volatility = float(bench["mom"]), float(bench["ann_vol"])
        regime = (
            Regime.R4_BEAR
            if momentum < 0
            else Regime.R2_BULL_HIGH_VOL
            if volatility > 0.25
            else Regime.R1_BULL_LOW_VOL
        )
        agent = TradingAgent(("momentum", "low_volatility"), default_regime_weight_config())
        scores = agent.compute_composite_scores(
            {
                symbol: {"momentum": float(row["mom"]), "low_volatility": -float(row["ann_vol"])}
                for symbol, row in rows.items()
            },
            regime,
        )
        features = {}
        for symbol, row in rows.items():
            previous = previous_rows[symbol]
            evaluation = evaluate_entry_signal(row, 2 if momentum > 0 else 0, cfg, momentum)
            close, atr = float(row["close"]), float(row["atr"])
            if not math.isfinite(atr) or atr <= 0:
                raise ValueError(f"{symbol} 的 ATR 无法支持风险预算")
            stop = evaluation.signal.stop if evaluation.signal else close - cfg["atr_stop_mult"] * atr
            features[symbol] = {
                "close": close,
                "atr": atr,
                "momentum": float(row["mom"]),
                "annualized_volatility": float(row["ann_vol"]),
                "average_dollar_volume": float(row["adv"]),
                "uptrend": bool(row["uptrend"]),
                "breakout": close > float(row["hh_break"]),
                "pullback": bool(row["pullback"]),
                "bullish_reversal": bool(row["reversal"]),
                "factor_score": scores[symbol],
                "pa_accepted": evaluation.signal is not None,
                "pa_reason": evaluation.reason_code,
                "suggested_stop": max(0.01, float(stop)),
                "channel_exit": close < float(row["ll_exit"]),
                "previous_rotation_eligible": bool(previous["mom"] > 0 and previous["uptrend"]
                                                   and not previous["close"] < previous["ll_exit"]),
            }
        payload = {
            "as_of": session_time(day).isoformat(),
            "bars": prefix,
            "features": features,
            "regime": regime.value,
            "strategy": cfg,
        }
        context = MarketContext(
            hash_payload(payload),
            session_time(day),
            features,
            regime.value,
            self.config["market"]["benchmark"],
        )
        self._features[key] = context
        return context

"""Crypto spot allocation using the shared LLM contract and Decimal paper ledger.

Only offline candles and mock instruments are supported. No exchange order API
is reachable from this module. Mock minimum sizes are also the quantity steps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import json
from pathlib import Path
import sqlite3

from quantbot.ai.config import config_hash
from quantbot.ai.domain import MarketContext, validate_decision
from quantbot.ai.gateway import decision_provider
from quantbot.ai.models import DemoProvider
from quantbot.ai.transport import ProviderError
from quantbot.evidence import hash_payload
from quantbot.crypto.runtime.exchanges.base import Candle
from quantbot.crypto.runtime.exchanges.mock import MockExchange
from quantbot.crypto.runtime.directional.indicators import atr as crypto_atr
from quantbot.crypto.runtime.directional.indicators import ema, rsi

D = Decimal
INTERVALS = {"15m": timedelta(minutes=15), "1h": timedelta(hours=1), "1d": timedelta(days=1)}


def number(value):
    if isinstance(value, bool):
        raise ValueError("数值不能为布尔值")
    try:
        result = D(str(value))
    except InvalidOperation:
        raise ValueError("无效数值") from None
    if not result.is_finite():
        raise ValueError("数值必须有限")
    return result


def candles_from_rows(rows):
    result = []
    for r in rows:
        timestamp = datetime.fromisoformat(str(r["timestamp"]).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("加密行情 timestamp 必须带时区")
        result.append(
            Candle(
                "offline",
                r["symbol"],
                r.get("bar", "15m"),
                *(number(r[k]) for k in ("open", "high", "low", "close", "volume")),
                timestamp.astimezone(UTC),
                r.get("complete", True),
            )
        )
    return result


def market_context(candles, symbols, *, now=None, source="offline_csv", atr_window=14, atr_stop_mult=2.5):
    now = now or datetime.now(UTC)
    selected = {s: [] for s in symbols}
    seen = set()
    for c in candles:
        if c.symbol not in selected:
            continue
        if c.bar not in INTERVALS or c.timestamp.tzinfo is None or c.complete is not True:
            raise ValueError("只接受带时区的完整 15m/1h/1d K 线")
        if c.timestamp + INTERVALS[c.bar] > now:
            raise ValueError("行情包含尚未收盘或未来的 K 线")
        key = (c.symbol, c.timestamp)
        if key in seen:
            raise ValueError("重复加密 K 线")
        seen.add(key)
        values = [c.open, c.high, c.low, c.close, c.volume]
        if any(not v.is_finite() for v in values) or min(values[:4]) <= 0 or c.volume < 0:
            raise ValueError("OHLCV 必须为有效非负数值，价格必须为正")
        if c.low > min(c.open, c.close) or c.high < max(c.open, c.close) or c.high < c.low:
            raise ValueError("K 线高低价无效")
        selected[c.symbol].append(c)
    features, canonical, ends = {}, {}, set()
    for symbol, history in selected.items():
        history.sort(key=lambda c: c.timestamp)
        minimum = max(35, atr_window + 1)
        if len(history) < minimum or len({c.bar for c in history}) != 1:
            raise ValueError(f"{symbol} 需要至少 {minimum} 根同周期 K 线")
        interval = INTERVALS[history[0].bar]
        if any(b.timestamp - a.timestamp != interval for a, b in zip(history, history[1:])):
            raise ValueError(f"{symbol} K 线时间不连续")
        last = history[-1]
        ends.add(last.timestamp + interval)
        atr = crypto_atr(history, atr_window)[-1]
        if atr <= 0 or last.close <= atr * number(atr_stop_mult):
            raise ValueError(f"{symbol} 的 ATR 不支持有效止损")
        momentum = last.close / history[-21].close - 1
        closes = [c.close for c in history]
        emas = {period: ema(closes, period) for period in (5, 10, 20, 30)}
        path_length = sum(abs(b - a) for a, b in zip(closes[-11:-1], closes[-10:]))
        confirmed = all(
            closes[i] > emas[20][i] * D("1.005")
            and emas[5][i] > emas[20][i] * D("1.003")
            and closes[i] / closes[i - 5] - 1 > D("0.005")
            for i in (-2, -1)
        )
        features[symbol] = {
            "close": float(last.close),
            "atr": float(atr),
            "momentum": float(momentum),
            "momentum_5": float(last.close / history[-6].close - 1),
            **{f"ema_{period}": float(values[-1]) for period, values in emas.items()},
            "efficiency_ratio_10": float(abs(closes[-1] - closes[-11]) / path_length) if path_length else 0.0,
            "trend_confirmed_2": confirmed,
            "rsi_14": float(rsi(closes, 14)[-1]),
            "prior_high_20": float(max(c.high for c in history[-21:-1])),
            "prior_low_10": float(min(c.low for c in history[-11:-1])),
            "indicator_definitions": {
                "momentum": {"lookback_bars": 20, "bar": last.bar,
                             "unit": "fraction", "formula": "close / close_20_bars_ago - 1"},
                "atr": {"lookback_bars": atr_window, "bar": last.bar, "unit": "USDT"},
                "momentum_5": {"lookback_bars": 5, "bar": last.bar, "unit": "fraction",
                               "formula": "close / close_5_bars_ago - 1"},
                "ema": {"periods": [5, 10, 20, 30], "bar": last.bar, "unit": "USDT",
                        "formula": "alpha=2/(period+1), seeded with first available close"},
                "rsi_14": {"lookback_bars": 14, "bar": last.bar, "unit": "0..100",
                           "formula": "Wilder smoothing; flat/no-loss history is 100"},
                "prior_high_20": {"lookback_bars": 20, "bar": last.bar, "unit": "USDT",
                                  "formula": "max(high) over previous 20 bars, excluding current bar"},
                "prior_low_10": {"lookback_bars": 10, "bar": last.bar, "unit": "USDT",
                                 "formula": "min(low) over previous 10 bars, excluding current bar"},
                "efficiency_ratio_10": {"lookback_bars": 10, "bar": last.bar, "unit": "0..1",
                                        "formula": "abs(close-close_10_ago) / sum(abs(change)); flat path=0"},
                "trend_confirmed_2": {"bar": last.bar, "unit": "boolean",
                                      "formula": "last 2 closes > EMA20*1.005, EMA5 > EMA20*1.003, momentum5 > 0.005"},
            },
            "uptrend": momentum > 0,
            "source": source,
            "bar": last.bar,
            "stop_candidate": float(last.close - atr * number(atr_stop_mult)),
            "quantity_unit": "base_asset",
            "quote_asset": "USDT",
        }
        canonical[symbol] = [c.to_dict() for c in history]
    if len(ends) != 1:
        raise ValueError("股票池每个加密标的必须对齐同一收盘时间")
    as_of = ends.pop()
    context = MarketContext(
        hash_payload({"source": source, "candles": canonical}), as_of, features, "crypto_spot", symbols[0]
    )
    return context, selected


@dataclass
class CryptoAccount:
    cash: Decimal
    positions: dict[str, Decimal]
    stops: dict[str, Decimal]
    take_profits: dict[str, Decimal] = field(default_factory=dict)
    last_exit_bar: dict[str, str] = field(default_factory=dict)

    def in_cooldown(self, context, symbol, bars):
        if not bars or symbol not in self.last_exit_bar:
            return False
        interval = INTERVALS[context.features[symbol]["bar"]]
        return context.as_of - datetime.fromisoformat(self.last_exit_bar[symbol]) < interval * bars

    def payload(self, context):
        if set(self.positions) - set(context.features):
            raise ValueError("账户资产不在本次行情池内")
        equity = self.cash + sum(q * number(context.prices[s]) for s, q in self.positions.items())
        return {
            "cash": float(self.cash),
            "equity": float(equity),
            "positions": {s: str(q) for s, q in self.positions.items()},
            "stops": {s: str(p) for s, p in self.stops.items()},
            "take_profits": {s: str(p) for s, p in self.take_profits.items()},
            "last_exit_bar": dict(self.last_exit_bar),
            "current_weights": {
                s: float(q * number(context.prices[s]) / equity) if equity else 0
                for s, q in self.positions.items()
            },
        }


def spot_plan(context, account, decision, config, instruments):
    allocations = validate_decision(
        {
            "snapshot_id": context.snapshot_id,
            "as_of": context.as_of.isoformat(),
            "decisions": [vars(a) for a in decision.allocations],
        },
        context,
    )
    risk = config["risk"]
    equity = number(account.payload(context)["equity"])
    cash = max(D(0), account.cash - equity * number(risk["cash_buffer"]))
    cost = number(config["execution"]["cost_bps"]) / D(10000)
    heat = max(
        D(0),
        equity * number(risk["max_portfolio_heat"])
        - sum(
            q * max(D(0), number(context.prices[s]) - account.stops.get(s, D(0)))
            for s, q in account.positions.items()
        ),
    )
    weights, adjustments = {}, []
    existing = sum(q > 0 for q in account.positions.values())
    slots = max(0, risk["max_positions"] - existing)
    for a in sorted(allocations, key=lambda a: (-a.confidence, a.symbol)):
        current = account.positions.get(a.symbol, D(0)) * number(context.prices[a.symbol]) / equity
        weight = min(number(a.target_weight), number(risk["max_weight"]))
        if a.confidence < risk["min_confidence"] and weight > current:
            weight = current
            adjustments.append(a.symbol + ":low_confidence")
        if not current and weight and account.in_cooldown(
            context, a.symbol, config["strategy"].get("crypto_cooldown_bars", 0)
        ):
            weight = D(0)
            adjustments.append(a.symbol + ":entry_cooldown")
        if not account.positions.get(a.symbol, 0) and weight:
            if slots <= 0:
                weight = D(0)
                adjustments.append(a.symbol + ":position_limit")
            else:
                slots -= 1
        weights[a.symbol] = weight
    total = sum(weights.values())
    if total > number(risk["max_gross_exposure"]):
        scale = number(risk["max_gross_exposure"]) / total
        weights = {s: w * scale for s, w in weights.items()}
        adjustments.append("gross_exposure_scaled")
    orders = []
    for symbol in sorted(weights):
        meta = instruments[symbol]
        step = meta.min_size
        price = number(context.prices[symbol])
        current = account.positions.get(symbol, D(0))
        target = equity * weights[symbol] / price
        if abs(target - current) < step / D(1000000):
            continue
        target = (target / step).to_integral_value(rounding=ROUND_DOWN) * step
        side = "buy" if target > current else "sell"
        qty = abs(target - current)
        if qty == 0:
            adjustments.append(symbol + ":quantity_step_no_change")
            continue
        stop = number(context.features[symbol]["stop_candidate"])
        if side == "buy":
            per_unit = price * (1 + cost)
            distance = per_unit - stop
            qty = min(
                qty, cash / per_unit, equity * number(risk["risk_per_trade"]) / distance, heat / distance
            )
            qty = (qty / step).to_integral_value(rounding=ROUND_DOWN) * step
            cash -= qty * per_unit
            heat -= qty * distance
        if qty <= 0:
            adjustments.append(symbol + ":cash_or_risk_budget_rejected")
            continue
        orders.append(
            {
                "symbol": symbol,
                "side": side,
                "quantity": str(qty),
                "price": str(price),
                "stop": str(stop),
                "quantity_step": str(step),
                "client_order_id": "qc-"
                + hash_payload({"snapshot": context.snapshot_id, "symbol": symbol, "side": side})[:32],
            }
        )
    return {
        "provider": decision.provider,
        "model": decision.model,
        "usage": decision.usage,
        "allocations": [vars(a) for a in allocations],
        "orders": orders,
        "adjustments": adjustments,
    }


def run_spot(
    config,
    *,
    symbols=None,
    candles=None,
    demo=False,
    mode="decide",
    initial_cash=1000,
    ledger_path=None,
    provider=None,
    source=None,
):
    if config["risk"].get("crypto_stop_loss_pct") or config["strategy"].get("crypto_cooldown_bars", 0):
        raise ValueError("固定止盈止损与冷却期需要使用统一 backtest/runtime 入口")
    symbols = symbols or ["BTC/USDT", "ETH/USDT"]
    exchange = MockExchange()
    instruments = {i.symbol: i for i in exchange.list_instruments("spot") if i.quote_asset == "USDT"}
    if not symbols or len(set(symbols)) != len(symbols) or set(symbols) - set(instruments):
        raise ValueError("当前加密 AI 模拟支持 mock 元数据中的 USDT 现货对")
    if mode not in {"decide", "paper"} or number(initial_cash) <= 0:
        raise ValueError("只支持 decide/paper，初始资金必须为正")
    if candles is None:
        candles = [c for s in symbols for c in exchange.get_candles(s, limit=100)]
        source = "mock_fixture"
    context, history = market_context(
        candles,
        symbols,
        source=source or "offline_csv",
        atr_window=config["strategy"]["atr_window"],
        atr_stop_mult=config["strategy"]["atr_stop_mult"],
    )
    identity = hash_payload(
        {
            "config": config_hash(config),
            "symbols": symbols,
            "initial_cash": str(initial_cash),
            "source": source or "offline_csv",
            "demo": demo,
        }
    )
    db = None
    try:
        account = CryptoAccount(number(initial_cash), {}, {})
        last = None
        if mode == "paper":
            if ledger_path is None:
                raise ValueError("paper 需要独立 SQLite 账户路径")
            path = Path(ledger_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(path, timeout=300)
            db.execute("CREATE TABLE IF NOT EXISTS account (id TEXT PRIMARY KEY, payload TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY, payload TEXT)")
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT payload FROM decisions WHERE id=?", (context.snapshot_id,)).fetchone()
            state = db.execute("SELECT id,payload FROM account").fetchone()
            if state and state[0] != identity:
                raise ValueError("账户配置或数据源已改变；使用新的 paper 账本")
            if prior:
                return {**json.loads(prior[0]), "idempotent_replay": True, "model_called": False}
            if state:
                values = json.loads(state[1])
                account = CryptoAccount(
                    number(values["cash"]),
                    {s: number(q) for s, q in values["positions"].items()},
                    {s: number(v) for s, v in values["stops"].items()},
                )
                last = datetime.fromisoformat(values["as_of"])
                if context.as_of <= last:
                    raise ValueError("禁止向 paper 账户回放更早或修订的 K 线")
        cost = number(config["execution"]["cost_bps"]) / D(10000)
        fills = []
        # Existing stops run before the model. New buys never use earlier candle lows.
        if last:
            if any(history[s][0].timestamp > last for s in account.positions):
                raise ValueError("行情未覆盖上次决策后的完整区间，无法校验保护止损")
            for symbol, qty in list(account.positions.items()):
                stop = account.stops.get(symbol)
                if not stop:
                    continue
                for c in history[symbol]:
                    if c.timestamp < last or c.low > stop:
                        continue
                    price = min(c.open, stop) * (1 - cost)
                    account.cash += qty * price
                    fills.append(
                        {
                            "symbol": symbol,
                            "side": "sell",
                            "quantity": str(qty),
                            "price": str(price),
                            "reason": "protective_stop",
                        }
                    )
                    account.positions.pop(symbol, None)
                    account.stops.pop(symbol, None)
                    break
        if not demo and config["strategy"]["mode"] != "ai":
            raise ValueError("加密 AI 入口只接受 ai 模式；规则策略使用 crypto strategy")
        selected = provider or (DemoProvider() if demo else decision_provider(config))
        error = None
        try:
            decision = selected.decide(context, account, config)
            plan = spot_plan(context, account, decision, config, instruments)
        except (ProviderError, ValueError):
            error = "模型不可用或输出未通过验证；本次没有新订单"
            plan = {
                "provider": "rejected",
                "model": config["llm"]["model"],
                "allocations": [],
                "orders": [],
                "adjustments": [error],
            }
        if mode == "paper":
            for order in sorted(plan["orders"], key=lambda o: o["side"] == "buy"):
                s, qty = order["symbol"], number(order["quantity"])
                price = number(order["price"]) * (1 + cost if order["side"] == "buy" else 1 - cost)
                if order["side"] == "buy":
                    if qty * price > account.cash:
                        raise ValueError("模拟资金预算不一致")
                    account.cash -= qty * price
                    account.positions[s] = account.positions.get(s, D(0)) + qty
                    account.stops[s] = max(account.stops.get(s, D(0)), number(order["stop"]))
                else:
                    account.cash += qty * price
                    account.positions[s] -= qty
                    if account.positions[s] == 0:
                        account.positions.pop(s)
                        account.stops.pop(s, None)
                fills.append({**order, "price": str(price), "reason": "allocation"})
        result = {
            "market": "crypto_spot",
            "mode": mode,
            "status": "hold" if error else "ok",
            "as_of": context.as_of.isoformat(),
            "snapshot_id": context.snapshot_id,
            "data_source": source or "offline_csv",
            "model_called": not demo,
            "execution_basis": "offline_close_with_cost",
            "pnl_basis": "simulated_mark_to_market",
            "plan": plan,
            "fills": fills,
            "account": account.payload(context),
        }
        if db:
            values = {
                "cash": str(account.cash),
                "positions": {s: str(q) for s, q in account.positions.items()},
                "stops": {s: str(p) for s, p in account.stops.items()},
                "as_of": context.as_of.isoformat(),
            }
            db.execute("INSERT OR REPLACE INTO account VALUES (?,?)", (identity, json.dumps(values)))
            db.execute("INSERT INTO decisions VALUES (?,?)", (context.snapshot_id, json.dumps(result)))
            db.commit()
        return result
    finally:
        if db:
            db.close()

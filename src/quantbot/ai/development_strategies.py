"""有限的开发研究假设；不注册运行时 provider、不调用模型或交易渠道。

候选及参数由下列不可变登记表固定，费用、仓位和保护订单仍由共用执行器管理。
股票使用冻结配置生成的现有特征（本轮 20 日动量 / 20 日突破 / 10 日退出），
并由执行器按交易日强制冷却；加密候选另用账户退出记录检查三根冷却。
"""

from __future__ import annotations

from dataclasses import dataclass

from .domain import Allocation, AllocationAccount, DecisionBatch, MarketContext
from .risk import stock_in_cooldown


@dataclass(frozen=True)
class DevelopmentCandidate:
    id: str
    market: str
    family: str
    entry: str
    exit: str
    symbols: tuple[str, ...] = ()
    filtered: bool = False
    cooldown_bars: int = 3
    rotation_margin: float = 0.01
    efficiency_floor: float = 0.30
    control: bool = False
    rsi_entry_ceiling: float = 45.0
    confirm_rotation_entry: bool = False


_CRYPTO = (
    DevelopmentCandidate("btc_breakout20", "crypto", "breakout",
                         "close > previous 20-bar high", "close < previous 10-bar low", ("BTC/USDT",)),
    DevelopmentCandidate("btc_breakout20_filtered", "crypto", "breakout",
                         "20-bar breakout AND trend_confirmed_2 AND efficiency_ratio_10 >= 0.30",
                         "close < previous 10-bar low", ("BTC/USDT",), filtered=True),
    DevelopmentCandidate("eth_momentum20", "crypto", "momentum",
                         "20-bar momentum > 0", "20-bar momentum <= 0", ("ETH/USDT",)),
    DevelopmentCandidate("eth_momentum20_filtered", "crypto", "momentum",
                         "20-bar momentum > 0 AND trend_confirmed_2 AND efficiency_ratio_10 >= 0.30",
                         "20-bar momentum <= 0", ("ETH/USDT",), filtered=True),
    DevelopmentCandidate("trend_pullback", "crypto", "pullback",
                         "EMA10 > EMA30 AND RSI14 < 45 AND close > EMA30",
                         "RSI14 > 60 OR EMA10 <= EMA30", ("BTC/USDT", "ETH/USDT")),
    DevelopmentCandidate("relative_rotation", "crypto", "rotation",
                         "positive 20-bar momentum AND two-bar trend confirmation AND efficiency >= 0.30; "
                         "switch only when challenger momentum exceeds incumbent by >= 0.01",
                         "close < EMA20 OR momentum <= 0 OR eligible stronger challenger",
                         ("BTC/USDT", "ETH/USDT")),
    DevelopmentCandidate("trend_pullback_rsi50", "crypto", "pullback",
                         "EMA10 > EMA30 AND RSI14 < 50 AND close > EMA30",
                         "RSI14 > 60 OR EMA10 <= EMA30", ("BTC/USDT", "ETH/USDT"),
                         rsi_entry_ceiling=50.0),
)

_STOCKS = (
    DevelopmentCandidate("trend_breakout", "stocks", "breakout",
                         "uptrend AND breakout AND momentum > 0", "channel_exit OR NOT uptrend"),
    DevelopmentCandidate("trend_pullback", "stocks", "pullback",
                         "uptrend AND pullback AND bullish_reversal AND momentum > 0",
                         "channel_exit OR NOT uptrend"),
    DevelopmentCandidate("relative_rotation", "stocks", "rotation",
                         "uptrend AND momentum > 0; strongest momentum, switch advantage >= 0.01",
                         "channel_exit OR NOT uptrend OR momentum <= 0 OR eligible stronger challenger"),
    DevelopmentCandidate("relative_rotation_confirmed", "stocks", "rotation",
                         "new targets eligible on current AND previous completed session: "
                         "momentum > 0 AND uptrend AND NOT channel_exit; "
                         "strongest current momentum, switch advantage >= 0.01",
                         "channel_exit OR NOT uptrend OR momentum <= 0 OR eligible stronger challenger",
                         confirm_rotation_entry=True),
)


def development_candidates(market: str) -> tuple[DevelopmentCandidate, ...]:
    if market == "crypto":
        return _CRYPTO
    if market == "stocks":
        return _STOCKS
    raise ValueError("开发研究仅支持 crypto/stocks")


def development_controls(market: str) -> tuple[DevelopmentCandidate, ...]:
    development_candidates(market)
    return (
        DevelopmentCandidate("cash", market, "cash", "never enter", "always cash", control=True),
        DevelopmentCandidate("momentum", market, "momentum_control",
                             "positive momentum requests max_weight on every decision",
                             "momentum <= 0", control=True),
    )


def development_provider(market: str, candidate_id: str) -> DevelopmentStrategy:
    candidates = (*development_candidates(market), *development_controls(market))
    for candidate in candidates:
        if candidate.id == candidate_id:
            return DevelopmentStrategy(candidate)
    raise ValueError(f"未登记的 {market} 开发候选：{candidate_id}")


class DevelopmentStrategy:
    def __init__(self, candidate: DevelopmentCandidate):
        self.candidate = candidate

    def decide(self, context: MarketContext, account: AllocationAccount, config: dict) -> DecisionBatch:
        candidate = self.candidate
        if (context.regime == "crypto_spot") != (candidate.market == "crypto"):
            raise ValueError("研究候选与行情市场不一致")
        universe = set(context.features)
        scope = set(candidate.symbols) if candidate.symbols else universe
        if scope - universe:
            raise ValueError("行情缺少研究候选声明的标的")
        current = account.payload(context)["current_weights"]
        if candidate.family == "rotation":
            selected = self._rotation(context, account, current, scope)
            weights = {symbol: current.get(symbol, 0) or config["risk"]["max_weight"]
                       if symbol == selected else 0 for symbol in universe}
        else:
            weights = {}
            for symbol, values in context.features.items():
                if symbol not in scope or candidate.family == "cash":
                    weights[symbol] = 0
                    continue
                if candidate.family == "momentum_control":
                    weights[symbol] = config["risk"]["max_weight"] if values["momentum"] > 0 else 0
                    continue
                entering, exiting = self._signals(values)
                held = current.get(symbol, 0)
                if exiting:
                    weights[symbol] = 0
                elif held:
                    weights[symbol] = held
                elif entering and not self._cooling(context, account, symbol):
                    weights[symbol] = config["risk"]["max_weight"]
                else:
                    weights[symbol] = 0
        allocations = tuple(Allocation(symbol, float(weights[symbol]), 1.0,
                            f"开发研究 {candidate.id}；确定性目标，独立执行器仍约束数量、冷却和保护订单")
                            for symbol in sorted(universe))
        return DecisionBatch(allocations, "development_rules", candidate.id)

    def _cooling(self, context, account, symbol):
        if self.candidate.market == "stocks":
            return stock_in_cooldown(context.as_of, account.last_exit_bar, symbol, self.candidate.cooldown_bars)
        return account.in_cooldown(context, symbol, self.candidate.cooldown_bars)

    def _signals(self, values):
        candidate = self.candidate
        if candidate.market == "stocks":
            entering = values["uptrend"] and values["momentum"] > 0
            if candidate.family == "breakout":
                entering = entering and values["breakout"]
            else:
                entering = entering and values["pullback"] and values["bullish_reversal"]
            return entering, values["channel_exit"] or not values["uptrend"]
        if candidate.family == "breakout":
            entering = values["close"] > values["prior_high_20"]
            exiting = values["close"] < values["prior_low_10"]
        elif candidate.family == "momentum":
            entering, exiting = values["momentum"] > 0, values["momentum"] <= 0
        else:
            entering = values["ema_10"] > values["ema_30"] and values["rsi_14"] < candidate.rsi_entry_ceiling \
                and values["close"] > values["ema_30"]
            exiting = values["rsi_14"] > 60 or values["ema_10"] <= values["ema_30"]
        if candidate.filtered:
            entering = entering and self._trend_filter(values)
        return entering, exiting

    def _trend_filter(self, values):
        return values["trend_confirmed_2"] and values["efficiency_ratio_10"] >= self.candidate.efficiency_floor

    def _rotation(self, context, account, current, scope):
        def valid(values):
            if self.candidate.market == "crypto":
                return values["momentum"] > 0 and values["close"] >= values["ema_20"]
            return values["momentum"] > 0 and values["uptrend"] and not values["channel_exit"]

        held = [symbol for symbol in scope if current.get(symbol, 0) and valid(context.features[symbol])]
        incumbent = min(held, key=lambda symbol: (-current[symbol], symbol)) if held else None
        eligible = [symbol for symbol in scope if valid(context.features[symbol])
                    and (self.candidate.market == "stocks" or self._trend_filter(context.features[symbol]))
                    and (current.get(symbol, 0) or not self.candidate.confirm_rotation_entry
                         or context.features[symbol]["previous_rotation_eligible"])
                    and not self._cooling(context, account, symbol)]
        challenger = min(eligible, key=lambda symbol: (-context.features[symbol]["momentum"], symbol)) \
            if eligible else None
        if challenger is None:
            return incumbent
        if incumbent is None or context.features[challenger]["momentum"] \
                - context.features[incumbent]["momentum"] >= self.candidate.rotation_margin:
            return challenger
        return incumbent

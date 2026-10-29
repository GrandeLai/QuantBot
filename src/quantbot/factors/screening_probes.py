"""每日超跌事件与 ETF 入场质量的四项固定筛选，不注册交易策略。"""

from copy import deepcopy
from dataclasses import replace
import math

from .mechanism_probes import (
    DividendReplanProvider, ProbeProvider, feature_panel, origins, valid_features,
)


FILTERS = ("crypto_oversold_trend", "crypto_recovery_confirmation",
           "stocks_recent_trend", "stocks_cost_buffer")
PREFIX = "screen_v1_"
DEFINITIONS = {
    FILTERS[0]: "daily oversold onset: z20 <= -2 and previous z20 > -2; retain return60 > 0 and close > SMA120",
    FILTERS[1]: "same daily oversold onset; wait at most 3 closes for z20 > -2 and close > previous close; one attempt per onset symbol",
    FILTERS[2]: "same five-asset monthly trend and bounded dividend replan; additionally total_return21 > 0",
    FILTERS[3]: "same five-asset monthly trend and bounded dividend replan; additionally total_return63 * 20/63 > 2 * (2 * 30/10000)",
}


def research_cards(crypto_base, stocks_base):
    cards = []
    for name in FILTERS:
        crypto = name.startswith("crypto")
        card = deepcopy(crypto_base if crypto else stocks_base)
        card.update(id=PREFIX+name, screening_filter=name, readiness="ex_post_diagnostic_only")
        card["definition"].update(screening_rule=DEFINITIONS[name], parameters_to_optimize=[])
        if crypto:
            card["definition"].update(origin_stride_sessions=1,
                origin_anchor="union of daily oversold episode onsets in BTC and ETH; same event for both branches",
                signal="zscore20 <= -2 and previous zscore20 > -2; only onset symbols eligible",
                exit="zscore20 >= 0 OR original protection OR original origin + 10 sessions",
                entry="immediate next-open control; candidate timing defined by screening_rule")
            card["controls"] = ["cash", "same daily onset symbols, immediate entry, same 10-session horizon"]
        else:
            card["controls"] = ["cash", "same five assets, same monthly origins, same bounded dividend replan; no extra filter"]
        cards.append(card)
    return cards


def screening_panel(snapshot):
    panel = feature_panel(snapshot)
    for symbol in snapshot.config["market"]["symbols"]:
        for index, at in enumerate(snapshot.times):
            value = panel[(at.isoformat(), symbol)]
            previous = panel[(snapshot.times[index-1].isoformat(), symbol)] if index else None
            value["previous_zscore20"] = previous["zscore20"] if previous else None
            value["previous_close"] = previous["close"] if previous else None
            value["tr21"] = (value["tr_index"] / panel[(snapshot.times[index-21].isoformat(), symbol)]["tr_index"]-1
                             if index >= 21 else None)
    return panel


def onset(value):
    return (value["zscore20"] is not None and value["previous_zscore20"] is not None
            and value["zscore20"] <= -2 < value["previous_zscore20"])


def screening_origins(card, snapshot, panel, window):
    selected = origins(card, snapshot.times, window)
    if card["market"] == "crypto":
        selected = [at for at in selected if any(onset(panel[(at.isoformat(), s)]) for s in card["symbols"])]
    return selected


def targets(card, panel, at, config, *, candidate):
    name = card["screening_filter"]
    if name not in FILTERS:
        raise ValueError("未知的冻结筛选因子")
    values = {s: panel[(at, s)] for s in card["symbols"]}
    extra = ("previous_zscore20", "previous_close", "return60", "sma120") if card["market"] == "crypto" else ("tr21",)
    if not all(valid_features(card["mechanism"], v) and all(v[k] is not None and math.isfinite(v[k])
                                                        for k in extra) for v in values.values()):
        return {}, False
    if card["market"] == "crypto":
        chosen = [s for s, v in values.items() if onset(v) and
                  (not candidate or name != FILTERS[0] or v["return60"] > 0 and v["close"] > v["sma120"])]
    else:
        chosen = [s for s, v in values.items() if v["tr63"] > 0 and v["tr_index"] > v["tr_sma126"]
                  and (not candidate or (v["tr21"] > 0 if name == FILTERS[2]
                                         else v["tr63"] * 20/63 > 2 * (2 * 30/10000)))]
    # Keep the control's per-symbol budget when filtering; excluded allocation stays cash.
    # With five assets this matters: survivor weights must not rise just because others fail.
    if card["market"] == "stocks" and candidate:
        base, _ = targets(card, panel, at, config, candidate=False)
        return {s: base[s] for s in chosen}, True
    weight = min(config["risk"]["max_weight"], config["risk"]["max_gross_exposure"]/len(chosen)) if chosen else 0
    return {s: weight for s in chosen}, True


class FilteredReplanProvider(DividendReplanProvider):
    """首次和除息重试均应用同一个冻结过滤条件。"""

    def __init__(self, card, panel, start, config, actions, *, candidate):
        self.candidate = candidate
        selected, valid = targets(card, panel, start, config, candidate=candidate)
        super().__init__(card, panel, start, selected, actions)
        self.features_valid = valid

    def targets_at(self, at, config):
        return targets(self.card, self.panel, at, config, candidate=self.candidate)


class RecoveryProvider(ProbeProvider):
    """只为原点超跌标的等待三个收盘；确认后一次请求，取消/止损后不再尝试。"""

    def __init__(self, card, panel, start, selected, *, features_valid):
        super().__init__(card, panel, start, {}, features_valid=features_valid)
        self.pending = dict(selected)
        self.elapsed = -1
        self.entry_attempts = []

    def decide(self, context, account, config):
        self.elapsed += 1
        decision = super().decide(context, account, config)
        if not 1 <= self.elapsed <= 3:
            return decision
        at = context.as_of.isoformat()
        confirmed = {}
        for symbol, weight in list(self.pending.items()):
            value = self.panel[(at, symbol)]
            if any(value[k] is None or not math.isfinite(value[k]) for k in
                   ("zscore20", "close", "previous_close")):
                self.features_valid = False
                continue
            if value["zscore20"] > -2 and value["close"] > value["previous_close"]:
                # A move already through the frozen exit level is not a new entry.
                if value["zscore20"] < 0:
                    confirmed[symbol] = weight
                    self.entry_attempts.append({"symbol": symbol, "signal_at": at, "elapsed": self.elapsed})
                del self.pending[symbol]
        return replace(decision, allocations=tuple(replace(a, target_weight=confirmed[a.symbol])
                       if a.symbol in confirmed else a for a in decision.allocations))


def provider_for(card, panel, at, config, actions, *, candidate):
    if card["market"] == "stocks":
        return FilteredReplanProvider(card, panel, at, config, actions, candidate=candidate)
    selected, valid = targets(card, panel, at, config, candidate=candidate)
    if candidate and card["screening_filter"] == FILTERS[1]:
        return RecoveryProvider(card, panel, at, selected, features_valid=valid)
    return ProbeProvider(card, panel, at, selected, features_valid=valid)

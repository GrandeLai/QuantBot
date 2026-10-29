"""Self-developed US equity factor primitives."""


def earnings_revision_factor(current_eps: float, previous_eps: float) -> float:
    if previous_eps == 0:
        return 0.0
    return current_eps / previous_eps - 1


def short_interest_pressure_factor(
    previous_short_volume: float,
    previous_total_volume: float,
    current_short_volume: float,
    current_total_volume: float,
) -> float:
    previous_ratio = previous_short_volume / previous_total_volume if previous_total_volume else 0.0
    current_ratio = current_short_volume / current_total_volume if current_total_volume else 0.0
    return -(current_ratio - previous_ratio)


def insider_activity_factor(buy_value: float, sell_value: float) -> float:
    total = buy_value + sell_value
    return (buy_value - sell_value) / total if total else 0.0


def smart_money_13f_factor(
    current_shares: float,
    previous_shares: float,
    fund_quality_weight: float = 1.0,
) -> float:
    if previous_shares == 0:
        return 0.0
    return (float(current_shares) / float(previous_shares) - 1.0) * float(fund_quality_weight)


def earnings_sentiment_factor(sentiment: float, guidance: str) -> float:
    guidance = guidance.lower()
    if guidance == "raised":
        multiplier = 1.3
    elif guidance == "lowered":
        multiplier = 0.7
    else:
        multiplier = 1.0
    return sentiment * multiplier


def option_iv_skew_factor(call_iv: float, put_iv: float) -> float:
    return float(call_iv) - float(put_iv)


def supply_chain_margin_pressure_factor(input_price_change: float, pricing_power: float) -> float:
    return float(pricing_power) - float(input_price_change)


def search_trend_attention_factor(current_search_volume: float, baseline_search_volume: float) -> float:
    if baseline_search_volume == 0:
        return 0.0
    return current_search_volume / baseline_search_volume - 1

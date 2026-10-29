"""Flat-at-open corporate actions for raw-price intraday replay.

History is adjusted only when an action becomes effective. This is not an
entitlement ledger: overnight inventory and outstanding orders are rejected.
"""

from decimal import Decimal


def check_cash_distributions(actions, prior_close):
    for action in actions:
        if action["type"] != "dividend":
            continue
        previous = prior_close.get(action["symbol"])
        if previous is None:
            raise ValueError("普通分红缺少此前完整会话收盘价，不能核实特殊除息边界")
        if Decimal(action["amount_per_share"]) >= Decimal(str(previous)) * Decimal("0.25"):
            raise ValueError("分红达到前收盘价的 25%，特殊除息规则尚不支持")


def adjust_history(actions, prior_close, ranges, volumes):
    """Convert earlier completed features to the effective session's share units."""
    check_cash_distributions(actions, prior_close)
    for action in actions:
        if action["type"] != "split":
            continue
        symbol, ratio = action["symbol"], float(action["ratio"])
        if symbol in prior_close:
            prior_close[symbol] /= ratio
        history = ranges[symbol]
        adjusted = [value / ratio for value in history]
        history.clear()
        history.extend(adjusted)
        for (owner, _), history in volumes.items():
            if owner == symbol:
                adjusted = [value * ratio for value in history]
                history.clear()
                history.extend(adjusted)


def open_actions(actions, state, orders, previous_volume, at):
    """Check before reading new prices; a failed flatten cannot be erased by an action."""
    if state["positions"] or orders:
        raise ValueError("公司行动日开盘前必须空仓且无待单；残仓或待单不支持跨日除权")
    events = []
    for action in actions:
        symbol = action["symbol"]
        if action["type"] == "split" and symbol in previous_volume:
            previous_volume[symbol] *= Decimal(action["ratio"])
        events.append({"action": action, "at": at.isoformat(), "position_quantity": "0",
                       "cash_change": "0", "basis": "flat_at_open_raw_price_intraday"})
    return events

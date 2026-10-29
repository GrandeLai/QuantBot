"""只读重建完整持仓周期；入场标签仅来自原买单的决策时点，不重跑策略。"""

from datetime import datetime
from decimal import Decimal
import gzip
import hashlib
from pathlib import Path

from .domain import strict_json
from .reconciliation import reconcile_replay


D = Decimal
ZERO = D(0)


def _amount(value):
    if isinstance(value, bool):
        raise ValueError("归因金额不能为布尔值")
    result = D(str(value))
    if not result.is_finite():
        raise ValueError("归因金额必须有限")
    return result


def _equal(actual, expected, field):
    actual, expected = _amount(actual), _amount(expected)
    if abs(actual - expected) > max(D("0.00000001"), abs(expected) * D("1e-15")):
        raise ValueError(f"归因账目不一致：{field}")


def _time(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("归因时间必须带时区")
    return result


def _load(run_path, journal_meta):
    run_path, journal_path = Path(run_path), Path(journal_meta["path"])
    run_bytes, compressed = run_path.read_bytes(), journal_path.read_bytes()
    compressed_hash = hashlib.sha256(compressed).hexdigest()
    if compressed_hash != journal_meta["sha256"]:
        raise ValueError("压缩日志哈希不一致")
    raw = gzip.decompress(compressed)
    raw_hash = hashlib.sha256(raw).hexdigest()
    if raw_hash != journal_meta["uncompressed_sha256"]:
        raise ValueError("解压日志哈希不一致")
    document = strict_json(run_bytes.decode("utf-8"))
    records = [strict_json(line) for line in raw.decode("utf-8").splitlines()]
    if (not records or records[0]["kind"] != "started"
            or records[0]["payload"].get("schema_version") != 1
            or records[-1]["kind"] != "completed"):
        raise ValueError("归因需要完整的版本化日志及 completed 终态")
    allowed = {"started", "account", "orders_canceled", "decision_started", "decision", "decision_error", "completed"}
    for index, record in enumerate(records):
        if (type(record.get("sequence")) is not int or record["sequence"] != index
                or record["kind"] not in allowed or not isinstance(record.get("payload"), dict)
                or index and record["kind"] == "started"
                or index < len(records) - 1 and record["kind"] == "completed"):
            raise ValueError("归因日志结构、顺序或终态无效")
    return document, records, {
        "run_path": str(run_path.resolve()), "run_sha256": hashlib.sha256(run_bytes).hexdigest(),
        "journal_path": str(journal_path.resolve()), "journal_sha256": compressed_hash,
        "uncompressed_sha256": raw_hash,
    }


def _signals(records):
    snapshots, decisions, orders, resolved = {}, {}, {}, set()
    for index, record in enumerate(records):
        data, kind = record["payload"], record["kind"]
        if kind == "decision_started":
            at = data["as_of"]
            if at in snapshots or data["market"]["as_of"] != at:
                raise ValueError("入场信号快照重复或时间不一致")
            _time(at)
            snapshots[at] = data["market"]
        elif kind in {"decision", "decision_error"}:
            at = data["as_of"]
            if at not in snapshots or at in resolved:
                raise ValueError("决策与起始信号快照未确切对齐")
            resolved.add(at)
            if kind == "decision_error":
                continue
            if data["snapshot_id"] != snapshots[at]["snapshot_id"]:
                raise ValueError("决策与信号 snapshot_id 不一致")
            decisions[at] = data
            for order in data["orders"]:
                identity = order["client_order_id"]
                if identity in orders:
                    raise ValueError("决策订单身份重复")
                orders[identity] = {"signal_at": at, "order": order, "sequence": index}
    if resolved != set(snapshots):
        raise ValueError("完整日志含未完成的决策")
    return snapshots, decisions, orders


def _ordinary_actions(records, market):
    for action in records[0]["payload"].get("corporate_actions", []):
        if (market != "stocks" or action.get("type") != "dividend"
                or action.get("distribution") != "ordinary_cash"
                or any(_amount(action.get(key, 0)) != 0 for key in (
                    "return_of_capital", "short_term_capital_gains", "long_term_capital_gains",
                    "liquidation_distribution"))):
            raise ValueError("入场归因只支持普通现金分红，拒绝拆股或特殊分配")


def _finish(episode, initial, last_index, price=None):
    mark_net = episode["quantity"] * price - episode["net_basis"] if price is not None else ZERO
    mark_gross = episode["quantity"] * price - episode["gross_basis"] if price is not None else ZERO
    net = episode["realized_net"] + mark_net + episode["dividends"]
    gross = episode["realized_gross"] + mark_gross + episode["dividends"]
    _equal(gross - net, episode["costs"], "周期费用")
    result = {key: episode[key] for key in (
        "symbol", "signal_at", "entry_at", "exit_at", "buy_fills", "sell_fills", "exit_reasons",
        "entry_order_id", "snapshot_id", "entry_signals")}
    result.update(
        net_pnl=float(net), gross_pnl=float(gross), costs=float(episode["costs"]),
        entry_reference_notional=float(episode["entry_reference_notional"]),
        net_return=float(net / episode["entry_reference_notional"]), account_contribution=float(net / initial),
        holding_bars=last_index - episode["entry_index"],
        gross_dividends=float(episode["dividends"]), quantity=str(episode["quantity"]),
        mixed_entry_signals=len({row["signal_at"] for row in episode["entry_signals"]}) > 1,
        realized_trade_net_pnl=float(episode["realized_net"]),
        unrealized_trade_net_pnl=float(mark_net),
    )
    return result, net


def reconstruct_entries(run_path, journal_meta, market):
    """返回 events/open_episodes/daily/source/accounting；金额输出为 JSON 可用数值。

    events 只包含从空仓到再次空仓的完整周期；首次买单确定 signal_at，后续加仓
    记录在 entry_signals。净收益分母为周期累计买入参考名义额，不是账户权益。
    分红于取得权益时归属原周期，支付只转现金。期末费用估计不伪造卖出事件。
    journal_meta 使用原报告的 journal 字典；所有读取都保留原文件。
    """
    document, records, source = _load(run_path, journal_meta)
    summary, inspection = document["summary"], document["inspection"]
    metadata = records[0]["payload"]
    if market not in {"crypto", "stocks"} or metadata["spec"]["market"] != market:
        raise ValueError("归因市场身份不一致")
    if inspection["status"] != "completed" or summary["reconciliation"]["status"] != "reconciled":
        raise ValueError("归因需要 completed/reconciled 的完整 run")
    _ordinary_actions(records, market)
    reconciliation = reconcile_replay(records)
    if reconciliation["status"] != "reconciled" or reconciliation != summary["reconciliation"]:
        raise ValueError("重新核验的账目与 summary 不一致")
    snapshots, decisions, signal_orders = _signals(records)
    accounts = [r["payload"] for r in records if r["kind"] == "account"]
    curve = [{"as_of": row["as_of"], **row["account"]} for row in accounts]
    if curve != document["equity_curve"]:
        raise ValueError("run 的账户曲线与日志不一致")
    fills = [order for row in accounts for order in row["orders"] if order["status"] == "filled"]
    if fills != [order for order in document["orders"] if order["status"] == "filled"]:
        raise ValueError("run 的成交明细与日志不一致")
    final, initial = records[-1]["payload"]["account"], _amount(metadata["config"]["execution"]["initial_cash"])
    final_equity = _amount(final["equity"])
    _equal(summary["cost_bps"], metadata["config"]["execution"]["cost_bps"], "cost_bps")
    final_sell_cost = sum((_amount(q) * _amount(accounts[-1]["valuation_prices"][s])
                           for s, q in final["positions"].items()), ZERO) * _amount(summary["cost_bps"]) / 10000
    checks = {"final_equity": final_equity, "costs": final["estimated_execution_cost"],
              "dividend_income": final["dividend_income"], "unpaid_dividend": final["dividend_receivable"],
              "return_fraction": final_equity / initial - 1, "estimated_final_sell_cost": final_sell_cost,
              "net_liquidation_return_estimate": (final_equity - final_sell_cost) / initial - 1,
              "fills": len(fills), "sells": sum(o["side"] == "sell" for o in fills),
              "bars": len(accounts), "decision_calls": len(snapshots)}
    for field, expected in checks.items():
        _equal(summary[field], expected, field)
    if (summary["remaining_positions"] != final["positions"]
            or summary["execution_checks"] != records[-1]["payload"]["checks"]
            or summary["execution_checks"]["no_risk_hold"] != (not summary["risk_hold"])
            or inspection["execution_completed"] != records[-1]["payload"]["execution_completed"]):
        raise ValueError("summary/inspection 与日志终态不一致")
    positions, closed, daily = {}, [], []
    closed_net, previous_equity, account_index = ZERO, initial, -1
    for sequence, record in enumerate(records):
        if record["kind"] != "account":
            continue
        row = record["payload"]
        account_index += 1
        timeline = [(o["at"], 1, "fill", o) for o in row["orders"] if o["status"] == "filled"]
        for event in row.get("corporate_actions", []):
            kind = event["kind"]
            if kind not in {"dividend_entitlement", "dividend_payment"}:
                raise ValueError("不支持的公司行动")
            timeline.append((event["at"], 2 if kind == "dividend_payment" else 0, kind, event))
        timeline.sort(key=lambda event: (_time(event[0]), event[1]))
        for _, _, kind, order in timeline:
            if kind == "dividend_payment":
                continue
            if kind == "dividend_entitlement":
                if _amount(order["amount"]):
                    positions[order["action"]["symbol"]]["dividends"] += _amount(order["amount"])
                continue
            symbol, quantity = order["symbol"], _amount(order["quantity"])
            price, reference, fee = (_amount(order[k]) for k in ("price", "reference_price", "estimated_cost"))
            if order["side"] == "buy":
                identity = order["client_order_id"]
                if identity not in signal_orders:
                    raise ValueError(f"买单缺少确切入场信号：{identity}")
                signal = signal_orders[identity]
                at, planned = signal["signal_at"], signal["order"]
                if (signal["sequence"] >= sequence or _time(at) > _time(order["at"])
                        or planned["side"] != "buy" or planned["symbol"] != symbol
                        or _amount(planned["quantity"]) != quantity
                        or "created_at" in order and order["created_at"] != at):
                    raise ValueError("买单与入场信号身份、数量或时间不一致")
                if symbol not in snapshots[at]["features"]:
                    raise ValueError("入场快照缺少买入标的")
                if symbol not in positions:
                    positions[symbol] = {"symbol": symbol, "signal_at": at, "entry_at": order["at"],
                        "exit_at": None, "entry_order_id": identity, "snapshot_id": snapshots[at]["snapshot_id"],
                        "entry_index": account_index, "buy_fills": 0, "sell_fills": 0, "exit_reasons": [],
                        "entry_signals": [], "quantity": ZERO, "net_basis": ZERO, "gross_basis": ZERO,
                        "realized_net": ZERO, "realized_gross": ZERO, "dividends": ZERO, "costs": ZERO,
                        "entry_reference_notional": ZERO}
                episode = positions[symbol]
                episode["entry_signals"].append({"signal_at": at, "entry_at": order["at"],
                                                "client_order_id": identity, "snapshot_id": snapshots[at]["snapshot_id"]})
                episode["quantity"] += quantity
                episode["net_basis"] += quantity * price
                episode["gross_basis"] += quantity * reference
                episode["entry_reference_notional"] += quantity * reference
                episode["costs"] += fee
                episode["buy_fills"] += 1
            else:
                episode = positions[symbol]
                fraction = quantity / episode["quantity"]
                episode["realized_net"] += quantity * price - episode["net_basis"] * fraction
                episode["realized_gross"] += quantity * reference - episode["gross_basis"] * fraction
                episode["net_basis"] *= 1 - fraction
                episode["gross_basis"] *= 1 - fraction
                episode["quantity"] -= quantity
                episode["costs"] += fee
                episode["sell_fills"] += 1
                reason = order.get("reason", "signal_exit")
                if fraction < 1 and reason not in {"protective_stop", "take_profit"}:
                    sell_signal = signal_orders.get(order["client_order_id"])
                    if (sell_signal is not None and f"{symbol}:max_weight_clamped" in
                            decisions[sell_signal["signal_at"]]["adjustments"]):
                        reason = "risk_reduction"
                episode["exit_reasons"].append(reason)
                if not episode["quantity"]:
                    episode["exit_at"] = order["at"]
                    event, net = _finish(episode, initial, account_index)
                    closed.append(event)
                    closed_net += net
                    del positions[symbol]
        equity = _amount(row["account"]["equity"])
        daily.append({"as_of": row["as_of"], "equity": float(equity),
                      "positions": row["account"]["positions"], "daily_pnl": float(equity - previous_equity),
                      "has_decision": row["as_of"] in snapshots, "is_final_bar": account_index == len(accounts) - 1})
        previous_equity = equity
    opened, open_net = [], ZERO
    for symbol, episode in positions.items():
        item, net = _finish(episode, initial, account_index, _amount(accounts[-1]["valuation_prices"][symbol]))
        item["estimated_final_sell_cost"] = float(episode["quantity"] *
            _amount(accounts[-1]["valuation_prices"][symbol]) * _amount(summary["cost_bps"]) / 10000)
        opened.append(item)
        open_net += net
    difference = closed_net + open_net - (final_equity - initial)
    _equal(difference, ZERO, "完整周期与未平仓市值合计")
    return {"events": closed, "open_episodes": opened, "daily": daily,
            "initial_equity": float(initial), "source": {**source, "code_hash": metadata["code_hash"]},
            "accounting": {"status": "reconciled", "account_snapshots": len(accounts),
                "closed_net_pnl": float(closed_net), "open_mark_to_market_net_pnl": float(open_net),
                "final_equity": float(final_equity), "difference": float(difference),
                "dividend_income": float(_amount(final["dividend_income"])),
                "dividend_receivable": float(_amount(final["dividend_receivable"])),
                "estimated_final_sell_cost": float(final_sell_cost), "risk_hold": summary["risk_hold"],
                "summary_consistent": True, "buy_signals_exact": True},
            "boundary": "首次买单冻结周期标签；多次买入另列。净收益分母为累计买入参考名义额。"
                        "分红含税前权益且支付不重复；未平仓独立列出，期末清算费用仅为估计。"}

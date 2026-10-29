"""共享 StockBot 订单/持仓账本和次日开盘撮合，补充确定性保护性止损。"""

from __future__ import annotations

from datetime import date
import math
from pathlib import Path

from quantbot.evidence import hash_payload
from quantbot.trading.domain import BrokerAccountSnapshot, FillEvent, MarginStatus, OrderIntent, OrderState
from quantbot.trading.engine import BacktestExecutionPort
from quantbot.trading.ledger import PortfolioLedger

from .domain import AccountState
from .journal import AIJournal
from .market import DailyMarketData, session_time
from .risk import build_plan
from .transport import ProviderError


def simulated_account(ledger: PortfolioLedger, stops=None) -> AccountState:
    state = ledger.snapshot()
    broker = BrokerAccountSnapshot(
        "quantbot_sim",
        ledger.observed_at,
        state.equity,
        max(0, state.cash),
        False,
        False,
        MarginStatus.SIMULATED,
    )
    return AccountState(
        max(0, state.cash),
        state.equity,
        {s: int(p.quantity) for s, p in state.positions.items()},
        broker,
        stops or {},
    )


def simulate(
    market: DailyMarketData,
    config: dict,
    provider,
    output: Path,
    start: date | None = None,
    end: date | None = None,
) -> dict:
    if output.exists() and any(output.iterdir()):
        raise ValueError("回放输出目录必须为空，避免覆盖或混用历史证据")
    output.mkdir(parents=True, exist_ok=True)
    dates = [
        day for day in market.common_dates if (start is None or day >= start) and (end is None or day <= end)
    ]
    valid_dates = []
    for day in dates:
        try:
            market.context(day)
        except ValueError as error:
            if "历史不足" in str(error):
                continue
            raise
        valid_dates.append(day)
    if len(valid_dates) < 2:
        raise ValueError("回放至少需要两个预热完成后的共同交易日")
    ledger = PortfolioLedger(
        config["execution"]["initial_cash"], session_time(valid_dates[0]), allow_short=False
    )
    port = BacktestExecutionPort(cost_bps=config["execution"]["cost_bps"])
    mode = (
        "ai_simulation"
        if provider.__class__.__name__ in {"OpenAICompatibleProvider", "RoutedDecisionProvider"}
        else "price_action_simulation"
        if config["strategy"]["mode"] == "price_action"
        else "demo_simulation"
    )
    identity = hash_payload({"mode": mode, "path": str(output.resolve())})
    stops, pending, reports, errors, equity_curve = {}, {}, [], [], []
    peak, max_drawdown = ledger.equity, 0.0
    with AIJournal(output / "journal.sqlite", config, mode, identity) as audit:
        for day in valid_dates:
            bars = market.bars(day)
            # Entry limits match the paper adapter: a gap beyond the budgeted limit gets canceled.
            for client_id, (intent, plan, run_id) in list(pending.items()):
                if (
                    intent.side == "buy"
                    and bars[intent.symbol].open * (1 + port.cost_bps / 10000)
                    > plan.limit_prices[intent.symbol]
                ):
                    port.cancel_order(client_id)
                    ledger.cancel_order(client_id)
                    terminal = audit.order(
                        run_id, intent, "canceled", metadata={"reason": "open_above_entry_limit"}
                    )
                    audit.commit_order(
                        run_id,
                        intent,
                        terminal,
                        simulated_account(ledger, stops).payload(market.context(day)),
                    )
                    del pending[client_id]
            for symbol in sorted(bars):
                for fill in port.on_event(bars[symbol]):
                    intent, plan, run_id = pending[fill.client_order_id]
                    quantity = math.floor(min(fill.quantity, ledger.fillable_quantity(fill)))
                    if quantity <= 0:
                        continue
                    if quantity < fill.quantity:
                        fill = port.resize_fill(fill, quantity)
                    ledger.apply_fill(fill)
                    port.acknowledge_fill(fill)
                    if intent.side == "buy":
                        stops[symbol] = max(stops.get(symbol, 0), plan.stops[symbol])
                    order_event = audit.order(
                        run_id,
                        intent,
                        "filled"
                        if ledger.order_state(intent.client_order_id) == OrderState.FILLED
                        else "partial_fill",
                        price=fill.price,
                        metadata={"fill_id": fill.fill_id, "fill_quantity": fill.quantity},
                    )
                    audit.journal.append_fill(
                        idempotency_key=fill.fill_id,
                        run_id=run_id,
                        order_id=order_event,
                        symbol=symbol,
                        side=fill.side,
                        quantity=int(fill.quantity),
                        price=fill.price,
                        occurred_at=fill.filled_at,
                    )
                    if ledger.order_state(intent.client_order_id) == OrderState.FILLED:
                        audit.commit_order(
                            run_id,
                            intent,
                            order_event,
                            simulated_account(ledger, stops).payload(market.context(day)),
                        )
                        del pending[intent.client_order_id]
            # All normal entry/exit orders are DAY orders; cancel any unfilled remainder.
            for client_id, (intent, _, run_id) in list(pending.items()):
                if intent.created_at >= session_time(day):
                    continue
                port.cancel_order(client_id)
                ledger.cancel_order(client_id)
                terminal = audit.order(run_id, intent, "canceled", metadata={"reason": "day_order_expired"})
                audit.commit_order(
                    run_id, intent, terminal, simulated_account(ledger, stops).payload(market.context(day))
                )
                del pending[client_id]
            # A buy fills at open before the bar's protective stop may trigger. A stop gap fills at open.
            for symbol, position in list(ledger.snapshot().positions.items()):
                stop = stops.get(symbol)
                if stop is None or bars[symbol].low > stop:
                    continue
                context = market.context(day)
                run_id = audit.run(context, "risk_exit", metadata={"symbol": symbol, "stop": stop})
                intent = OrderIntent(
                    "qs-" + hash_payload({"account": identity, "day": str(day), "symbol": symbol})[:32],
                    symbol,
                    "sell",
                    position.quantity,
                    bars[symbol].timestamp,
                )
                ledger.record_order(intent)
                ledger.accept_order(intent.client_order_id)
                fill = FillEvent(
                    "stop-" + intent.client_order_id,
                    intent.client_order_id,
                    symbol,
                    "sell",
                    position.quantity,
                    min(bars[symbol].open, stop) * (1 - port.cost_bps / 10000),
                    0.0,
                    session_time(day),
                )
                ledger.apply_fill(fill)
                terminal = audit.order(
                    run_id, intent, "filled", price=fill.price, metadata={"source": "risk_override"}
                )
                audit.journal.append_fill(
                    idempotency_key=fill.fill_id,
                    run_id=run_id,
                    order_id=terminal,
                    symbol=symbol,
                    side="sell",
                    quantity=int(fill.quantity),
                    price=fill.price,
                    occurred_at=fill.filled_at,
                )
                audit.commit_order(
                    run_id, intent, terminal, simulated_account(ledger, stops).payload(context)
                )
                stops.pop(symbol, None)
            ledger.mark_to_market(
                {symbol: bar.close for symbol, bar in bars.items()}, observed_at=session_time(day)
            )
            context = market.context(day)
            account = simulated_account(ledger, stops)
            peak = max(peak, account.equity)
            max_drawdown = min(max_drawdown, account.equity / peak - 1)
            equity_curve.append({"date": str(day), "equity": account.equity, "cash": account.cash})
            if day == valid_dates[-1]:
                run_id = audit.run(context, "completed", metadata={"last_bar_no_new_order": True})
                audit.equity(run_id, account)
                continue
            try:
                decision = provider.decide(context, account, config)
                plan = build_plan(context, account, decision, config, identity=identity)
            except (ProviderError, ValueError) as error:
                errors.append({"date": str(day), "error": str(error), "action": "hold"})
                run_id = audit.run(context, "model_error_hold", metadata=errors[-1])
                audit.equity(run_id, account)
                continue
            run_id = audit.plan(context, plan)
            audit.equity(run_id, account)
            reports.append({"date": str(day), "snapshot_id": context.snapshot_id, **plan.payload()})
            for intent in plan.intents:
                ledger.record_order(intent)
                port.submit_order(intent)
                ledger.accept_order(intent.client_order_id)
                audit.order(run_id, intent, "submitted", price=context.prices[intent.symbol])
                pending[intent.client_order_id] = (intent, plan, run_id)
        ledger.assert_invariants()
        result = {
            "run_type": mode,
            "provider_calls": len(reports) + len(errors),
            "real_model_calls": len(reports) + len(errors) if mode == "ai_simulation" else 0,
            "initial_equity": config["execution"]["initial_cash"],
            "final_equity": ledger.equity,
            "return_fraction": ledger.equity / config["execution"]["initial_cash"] - 1,
            "max_drawdown_fraction": max_drawdown,
            "fills": len(ledger.fills),
            "decision_days": len(reports),
            "errors": errors,
            "positions": simulated_account(ledger, stops).positions,
            "equity_curve": equity_curve,
            "decisions": reports,
            "evidence_boundary": "历史模型回放可能受训练记忆影响；演示/回放不代表前瞻收益或实盘验证",
        }
    return result

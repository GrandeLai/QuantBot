"""Tests for stateful hedged-maker paper lifecycle simulation."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from quantbot.crypto.runtime.config.loader import load_settings
from quantbot.crypto.runtime.exchanges.factory import ExchangeFactory
from quantbot.crypto.runtime.strategies.hedged_maker import HedgedMakerStrategyService
from quantbot.crypto.runtime.strategies.hedged_maker_lifecycle import HedgedMakerPaperLifecycleService
from quantbot.crypto.runtime.strategies.policy import StrategyPolicy


ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_CONFIG = ROOT / "config" / "crypto" / "config.example.yaml"
NOW = datetime(2026, 5, 16, 0, 0, tzinfo=UTC)


def test_hedged_maker_lifecycle_creates_open_paper_quote(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    opportunity = _opportunity(settings)
    service = HedgedMakerPaperLifecycleService(settings, ExchangeFactory(settings))

    result = service.apply(opportunity, StrategyPolicy(settings.strategy_runtime).lifecycle_plan(), now=NOW)

    assert result.status == "quoted"
    assert result.paper_only is True
    assert result.orders_sent is False
    assert result.live_orders_sent is False
    assert result.realized_net_profit_usdt == Decimal("0")
    state = service.read_state()
    open_orders = [order for order in state["orders"] if order["status"] == "open"]
    assert len(open_orders) == 1
    assert open_orders[0]["opportunity_id"] == opportunity.opportunity_id
    assert open_orders[0]["maker_order_type"] == "limit_post_only"


def test_hedged_maker_lifecycle_keeps_existing_quote_inside_reprice_threshold(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    opportunity = _opportunity(settings)
    service = HedgedMakerPaperLifecycleService(settings, ExchangeFactory(settings))
    lifecycle = StrategyPolicy(settings.strategy_runtime).lifecycle_plan()

    first = service.apply(opportunity, lifecycle, now=NOW)
    second = service.apply(opportunity, lifecycle, now=NOW + timedelta(seconds=5))

    assert first.status == "quoted"
    assert second.status == "active_quote_unchanged"
    assert second.active_order_count == 1
    state = service.read_state()
    assert len([order for order in state["orders"] if order["status"] == "open"]) == 1


def test_hedged_maker_lifecycle_replaces_expired_quote(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    opportunity = _opportunity(settings)
    service = HedgedMakerPaperLifecycleService(settings, ExchangeFactory(settings))
    lifecycle = StrategyPolicy(settings.strategy_runtime).lifecycle_plan()

    first = service.apply(opportunity, lifecycle, now=NOW)
    second = service.apply(opportunity, lifecycle, now=NOW + timedelta(seconds=settings.strategy_runtime.order_ttl_seconds + 1))

    assert second.status == "replaced_after_ttl"
    assert first.paper_order["order_id"] in second.canceled_order_ids
    state = service.read_state()
    assert len([order for order in state["orders"] if order["status"] == "canceled"]) == 1
    assert len([order for order in state["orders"] if order["status"] == "open"]) == 1


def test_hedged_maker_lifecycle_fills_existing_crossed_quote_and_records_hedge(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.hedged_maker.paper_queue_ahead_pct = Decimal("0")
    opportunity = _opportunity(settings)
    state_path = Path(settings.hedged_maker.paper_state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "orders": [
                    {
                        "order_id": "seed-crossed-buy",
                        "opportunity_id": opportunity.opportunity_id,
                        "symbol": opportunity.symbol,
                        "maker_exchange": "mock",
                        "hedge_exchange": "mock_alt",
                        "maker_side": "buy",
                        "hedge_side": "sell",
                        "maker_order_type": "limit_post_only",
                        "hedge_order_type": "taker_market_preview",
                        "price": "60000",
                        "quantity": "0.002",
                        "expected_net_profit_usdt": "0.55",
                        "status": "open",
                        "created_at": NOW.isoformat(),
                        "updated_at": NOW.isoformat(),
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    service = HedgedMakerPaperLifecycleService(settings, ExchangeFactory(settings))

    result = service.apply(
        opportunity,
        StrategyPolicy(settings.strategy_runtime).lifecycle_plan(),
        now=NOW + timedelta(seconds=1),
    )

    assert result.status == "filled_and_hedged"
    assert result.paper_order["order_id"] == "seed-crossed-buy"
    assert result.hedge_order is not None
    assert result.hedge_order["side"] == "sell"
    assert result.realized_net_profit_usdt < Decimal("0")
    state = service.read_state()
    filled_orders = [order for order in state["orders"] if order["status"] == "filled"]
    assert len(filled_orders) == 1
    assert filled_orders[0]["realized_net_profit_usdt"] == str(result.realized_net_profit_usdt)


def test_hedged_maker_lifecycle_partially_fills_after_queue_position(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.hedged_maker.paper_queue_ahead_pct = Decimal("75")
    settings.hedged_maker.paper_min_fill_pct = Decimal("25")
    opportunity = _opportunity(settings)
    _write_seed_order(settings, opportunity, price="60000", quantity="0.002")
    service = HedgedMakerPaperLifecycleService(settings, ExchangeFactory(settings))

    result = service.apply(
        opportunity,
        StrategyPolicy(settings.strategy_runtime).lifecycle_plan(),
        now=NOW + timedelta(seconds=1),
    )

    assert result.status == "partially_filled_and_hedged"
    assert result.hedge_order is not None
    assert Decimal(str(result.hedge_order["quantity"])) == Decimal("0.0005")
    assert result.paper_order["status"] == "partial_open"
    assert Decimal(str(result.paper_order["filled_quantity"])) == Decimal("0.0005")
    assert Decimal(str(result.paper_order["remaining_quantity"])) == Decimal("0.0015")
    state = service.read_state()
    assert len([order for order in state["orders"] if order["status"] == "partial_open"]) == 1


def test_hedged_maker_lifecycle_requotes_stale_quote_without_counting_as_fill(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.hedged_maker.paper_stale_quote_seconds = 1
    opportunity = _opportunity(settings)
    _write_seed_order(settings, opportunity, price="49900", quantity="0.002")
    service = HedgedMakerPaperLifecycleService(settings, ExchangeFactory(settings))

    result = service.apply(
        opportunity,
        StrategyPolicy(settings.strategy_runtime).lifecycle_plan(),
        now=NOW + timedelta(seconds=2),
    )

    assert result.status == "stale_requoted"
    assert result.canceled_order_ids == ["seed-buy"]
    assert result.realized_net_profit_usdt == Decimal("0")
    state = service.read_state()
    assert len([order for order in state["orders"] if order["status"] == "canceled"]) == 1
    assert len([order for order in state["orders"] if order["status"] == "open"]) == 1


def test_hedged_maker_lifecycle_cancel_latency_leaves_cancel_pending_before_requote(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.hedged_maker.paper_stale_quote_seconds = 1
    settings.hedged_maker.paper_cancel_latency_seconds = 5
    opportunity = _opportunity(settings)
    _write_seed_order(settings, opportunity, price="49900", quantity="0.002")
    service = HedgedMakerPaperLifecycleService(settings, ExchangeFactory(settings))
    lifecycle = StrategyPolicy(settings.strategy_runtime).lifecycle_plan()

    pending = service.apply(opportunity, lifecycle, now=NOW + timedelta(seconds=2))
    still_pending = service.apply(opportunity, lifecycle, now=NOW + timedelta(seconds=4))
    replaced = service.apply(opportunity, lifecycle, now=NOW + timedelta(seconds=8))

    assert pending.status == "cancel_pending"
    assert pending.paper_order["status"] == "cancel_pending"
    assert pending.paper_order["cancel_effective_at"] == (NOW + timedelta(seconds=7)).isoformat()
    assert still_pending.status == "cancel_pending"
    assert still_pending.paper_order["cancel_effective_at"] == (NOW + timedelta(seconds=7)).isoformat()
    assert replaced.status == "stale_requoted"
    assert replaced.canceled_order_ids == ["seed-buy"]
    state = service.read_state()
    assert len([order for order in state["orders"] if order["status"] == "canceled"]) == 1
    assert len([order for order in state["orders"] if order["status"] == "open"]) == 1


def test_hedged_maker_lifecycle_marks_adverse_selection_and_expands_hedge_slippage(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.hedged_maker.paper_queue_ahead_pct = Decimal("0")
    settings.hedged_maker.paper_adverse_selection_buffer_pct = Decimal("0.05")
    settings.hedged_maker.paper_adverse_hedge_slippage_multiplier = Decimal("3")
    opportunity = _opportunity(settings)
    _write_seed_order(settings, opportunity, price="60000", quantity="0.002")
    service = HedgedMakerPaperLifecycleService(settings, ExchangeFactory(settings))

    result = service.apply(
        opportunity,
        StrategyPolicy(settings.strategy_runtime).lifecycle_plan(),
        now=NOW + timedelta(seconds=1),
    )

    assert result.status == "filled_and_hedged"
    assert result.hedge_order is not None
    assert result.hedge_order["adverse_selection"] is True
    assert result.hedge_order["hedge_slippage_multiplier"] == "3"
    assert result.hedge_order["effective_hedge_slippage_pct"] == str(settings.hedged_maker.hedge_slippage_pct * Decimal("3"))


def test_hedged_maker_lifecycle_budget_state_counts_active_non_expired_quotes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    opportunity = _opportunity(settings)
    lifecycle = StrategyPolicy(settings.strategy_runtime).lifecycle_plan()
    state_path = Path(settings.hedged_maker.paper_state_path)
    state_path.write_text(
        json.dumps(
            {
                "orders": [
                    _paper_order(
                        order_id="matching-open",
                        symbol="BTC/USDT",
                        status="open",
                        price="50000",
                        quantity="0.002",
                        created_at=NOW.isoformat(),
                    ),
                    _paper_order(
                        order_id="partial-eth",
                        symbol="ETH/USDT",
                        status="partial_open",
                        price="3000",
                        quantity="0.02",
                        remaining_quantity="0.01",
                        created_at=NOW.isoformat(),
                    ),
                    _paper_order(
                        order_id="pending-future",
                        symbol="SOL/USDT",
                        status="cancel_pending",
                        price="100",
                        quantity="0.5",
                        created_at=NOW.isoformat(),
                        cancel_effective_at=(NOW + timedelta(seconds=5)).isoformat(),
                    ),
                    _paper_order(
                        order_id="pending-complete",
                        symbol="XRP/USDT",
                        status="cancel_pending",
                        price="1",
                        quantity="50",
                        created_at=NOW.isoformat(),
                        cancel_effective_at=(NOW - timedelta(seconds=1)).isoformat(),
                    ),
                    _paper_order(
                        order_id="expired-open",
                        symbol="ADA/USDT",
                        status="open",
                        price="1",
                        quantity="100",
                        created_at=(NOW - timedelta(seconds=31)).isoformat(),
                    ),
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    service = HedgedMakerPaperLifecycleService(settings, ExchangeFactory(settings))

    budget_state = service.budget_state(opportunity, lifecycle, now=NOW + timedelta(seconds=1))

    assert budget_state.active_orders == 3
    assert budget_state.active_capital_usdt == Decimal("180.00")
    assert budget_state.matching_active_order is True
    assert budget_state.active_order_ids == ["matching-open", "partial-eth", "pending-future"]


def _settings(tmp_path: Path):
    settings = load_settings(EXAMPLE_CONFIG)
    settings.hedged_maker.paper_state_path = str(tmp_path / "hedged-maker-paper-state.json")
    settings.strategy_runtime.order_ttl_seconds = 30
    settings.strategy_runtime.reprice_threshold_pct = Decimal("0.05")
    return settings


def _opportunity(settings):
    return HedgedMakerStrategyService(settings, ExchangeFactory(settings)).scan(
        symbol="BTC/USDT",
        maker_exchange="mock",
        hedge_exchange="mock_alt",
    )[0]


def _paper_order(
    *,
    order_id: str,
    symbol: str,
    status: str,
    price: str,
    quantity: str,
    created_at: str,
    remaining_quantity: str | None = None,
    cancel_effective_at: str | None = None,
) -> dict[str, str]:
    order = {
        "order_id": order_id,
        "opportunity_id": f"seed-{order_id}",
        "symbol": symbol,
        "maker_exchange": "mock",
        "hedge_exchange": "mock_alt",
        "maker_side": "buy",
        "hedge_side": "sell",
        "maker_order_type": "limit_post_only",
        "hedge_order_type": "taker_market_preview",
        "price": price,
        "quantity": quantity,
        "expected_net_profit_usdt": "0.55",
        "status": status,
        "created_at": created_at,
        "updated_at": created_at,
    }
    if remaining_quantity is not None:
        order["remaining_quantity"] = remaining_quantity
    if cancel_effective_at is not None:
        order["cancel_effective_at"] = cancel_effective_at
    return order


def _write_seed_order(settings, opportunity, *, price: str, quantity: str) -> None:
    state_path = Path(settings.hedged_maker.paper_state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "orders": [
                    {
                        "order_id": "seed-buy",
                        "opportunity_id": opportunity.opportunity_id,
                        "symbol": opportunity.symbol,
                        "maker_exchange": "mock",
                        "hedge_exchange": "mock_alt",
                        "maker_side": "buy",
                        "hedge_side": "sell",
                        "maker_order_type": "limit_post_only",
                        "hedge_order_type": "taker_market_preview",
                        "price": price,
                        "quantity": quantity,
                        "expected_net_profit_usdt": "0.55",
                        "status": "open",
                        "created_at": NOW.isoformat(),
                        "updated_at": NOW.isoformat(),
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

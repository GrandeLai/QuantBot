"""Unit tests for arbitrage models and scanners."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from quantbot.crypto.runtime.arbitrage.calculator import (
    calculate_funding_carry,
    calculate_fee,
    calculate_latency_drift_cost,
    calculate_risk_score,
    calculate_slippage,
    calculate_spread_persistence,
    calculate_tiered_fee,
    calculate_transfer_cost,
    estimate_depth_fill,
    has_sufficient_depth,
)
from quantbot.crypto.runtime.arbitrage.scanner import ArbitrageScanner
from quantbot.crypto.runtime.config.loader import load_settings
from quantbot.crypto.runtime.exchanges.factory import ExchangeFactory


ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_CONFIG = ROOT / "config" / "crypto" / "config.example.yaml"


def test_fee_slippage_depth_and_risk_calculations() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    factory = ExchangeFactory(settings)
    orderbook = factory.get("mock").get_orderbook("BTC/USDT")

    assert calculate_fee(Decimal("1000"), Decimal("0.001"), legs=2) == Decimal("2.000")
    assert calculate_slippage(Decimal("1000"), Decimal("0.0005"), legs=2) == Decimal("1.0000")
    assert has_sufficient_depth(orderbook, "ask", Decimal("1000")) is True
    assert calculate_risk_score(Decimal("0.8"), Decimal("0.02"), Decimal("150000")) < Decimal("0.5")


def test_execution_quality_calculations_cover_depth_fees_transfer_and_latency() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    orderbook = ExchangeFactory(settings).get("mock").get_orderbook("BTC/USDT")

    depth_fill = estimate_depth_fill(orderbook, "ask", Decimal("125000"))

    assert depth_fill.complete is True
    assert depth_fill.filled_notional == Decimal("125000")
    assert depth_fill.average_price > orderbook.best_ask.price
    assert depth_fill.slippage_pct > Decimal("0")
    assert calculate_tiered_fee(
        Decimal("1000"),
        maker_fee_pct=Decimal("0.0008"),
        taker_fee_pct=Decimal("0.001"),
        maker_ratio=Decimal("0.25"),
    ) == Decimal("0.9500")
    assert calculate_transfer_cost(
        Decimal("1000"),
        fixed_withdrawal_fee_usdt=Decimal("5"),
        delay_risk_pct=Decimal("0.001"),
    ) == Decimal("6.000")
    assert calculate_latency_drift_cost(
        Decimal("1000"),
        latency_ms=2500,
        drift_bps_per_second=Decimal("2"),
    ) == Decimal("0.50000")


def test_spread_persistence_and_funding_carry_calculations() -> None:
    persistence = calculate_spread_persistence(
        [Decimal("0.31"), Decimal("0.28"), Decimal("0.24")],
        min_spread_pct=Decimal("0.15"),
        required_windows=3,
    )
    carry = calculate_funding_carry(
        capital=Decimal("1000"),
        funding_rate=Decimal("0.003"),
        holding_hours=Decimal("24"),
        settlement_interval_hours=Decimal("8"),
        hedge_cost_pct=Decimal("0.0005"),
    )

    assert persistence.passed is True
    assert persistence.window_count == 3
    assert persistence.minimum_spread_pct == Decimal("0.24")
    assert carry.projected_funding_usdt == Decimal("9.000")
    assert carry.holding_cost_usdt == Decimal("0.5000")
    assert carry.net_carry_usdt == Decimal("8.5000")
    assert carry.annualized_pct > Decimal("300")


def test_cross_exchange_scanner_returns_profitable_mock_opportunity() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    scanner = ArbitrageScanner(settings, ExchangeFactory(settings))

    opportunities = scanner.scan("cross-exchange", symbol="BTC/USDT")

    assert opportunities
    opportunity = opportunities[0]
    assert opportunity.opportunity_id == "test-opportunity"
    assert opportunity.strategy_type == "cross-exchange"
    assert opportunity.buy_exchange == "mock"
    assert opportunity.sell_exchange == "mock_alt"
    assert opportunity.net_profit > Decimal("0")
    quality = opportunity.metadata["execution_quality"]
    assert quality["spread_persistence"]["passed"] is True
    assert quality["depth_fill"]["buy"]["complete"] is True
    assert quality["depth_fill"]["sell"]["complete"] is True
    assert quality["fee_model"]["taker_fee_pct"] == settings.arbitrage.taker_fee_pct
    assert quality["transfer_cost_usdt"] > Decimal("0")
    assert quality["latency_drift_usdt"] > Decimal("0")
    assert opportunity.to_dict()["net_profit"]


def test_triangular_funding_and_spot_perp_scanners_return_mock_opportunities() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    scanner = ArbitrageScanner(settings, ExchangeFactory(settings))

    triangular = scanner.scan("triangular", exchange="mock")
    funding = scanner.scan("funding-rate")
    spot_perp = scanner.scan("spot-perp", symbol="BTC/USDT")

    assert triangular[0].strategy_type == "triangular"
    assert triangular[0].net_profit > Decimal("0")
    assert funding[0].strategy_type == "funding-rate"
    assert funding[0].expected_profit_pct > Decimal("0")
    assert funding[0].metadata["funding_carry"]["annualized_pct"] > Decimal("100")
    assert funding[0].metadata["funding_carry"]["net_carry_usdt"] > Decimal("0")
    assert spot_perp[0].strategy_type == "spot-perp"
    assert spot_perp[0].net_profit > Decimal("0")


def test_new_okx_first_arbitrage_scanners_return_paper_opportunities() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    scanner = ArbitrageScanner(settings, ExchangeFactory(settings))

    triangular = scanner.scan("triangular-multi-route", exchange="mock")
    funding_carry = scanner.scan("funding-carry-hedged")
    spot_perp_carry = scanner.scan("spot-perp-carry", symbol="BTC/USDT")
    futures_basis = scanner.scan("futures-perp-basis", symbol="BTC/USDT")

    assert [item.metadata["route"] for item in triangular] == [
        ["USDT", "BTC", "ETH", "USDT"],
        ["USDT", "BTC", "SOL", "USDT"],
    ]
    assert triangular[0].strategy_type == "triangular-multi-route"
    assert triangular[0].metadata["route_depth_usdt"] >= settings.arbitrage.min_route_depth_usdt
    assert triangular[0].metadata["preflight_quality"]["net_positive_after_precision"] is True
    assert all("expected_fill_price" in leg for leg in triangular[0].metadata["legs"])
    assert all("submitted_limit_price" in leg for leg in triangular[0].metadata["legs"])
    assert all("min_size_adjustment" in leg for leg in triangular[0].metadata["legs"])
    assert funding_carry[0].strategy_type == "funding-carry-hedged"
    assert funding_carry[0].metadata["basis_hedge_cost_usdt"] >= Decimal("0")
    assert spot_perp_carry[0].strategy_type == "spot-perp-carry"
    assert spot_perp_carry[0].metadata["close_conditions"]["risk_or_drawdown_triggered"] is True
    assert futures_basis[0].strategy_type == "futures-perp-basis"
    assert futures_basis[0].metadata["days_to_expiry"] >= settings.arbitrage.futures_basis_min_days_to_expiry
    assert all(item.net_profit > Decimal("0") for item in [triangular[0], funding_carry[0], spot_perp_carry[0], futures_basis[0]])

"""Unit tests for trading assistant configuration loading."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.crypto.runtime.config.loader import load_settings
from quantbot.crypto.runtime.exceptions import ConfigError


ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_CONFIG = ROOT / "config" / "crypto" / "config.example.yaml"
OKX_DEMO_CONFIG = ROOT / "config" / "crypto" / "okx.demo.example.yaml"
OKX_LIVE_CONFIG = ROOT / "tests" / "crypto" / "fixtures" / "okx.live.yaml"
OKX_CONFIG_ENV_NAMES = (
    "QUANTBOT_CRYPTO_OKX_API_KEY",
    "QUANTBOT_CRYPTO_OKX_API_SECRET",
    "QUANTBOT_CRYPTO_OKX_PASSPHRASE",
    "QUANTBOT_CRYPTO_OKX_DEMO",
    "QUANTBOT_CRYPTO_AGENT_OPERATOR_ID",
    "QUANTBOT_CRYPTO_AGENT_LIVE_KILL_SWITCH",
    "QUANTBOT_CRYPTO_AGENT_TRADING_ENABLED",
    "QUANTBOT_CRYPTO_AGENT_ALLOW_DEMO_ORDERS",
    "QUANTBOT_CRYPTO_AGENT_ALLOW_LIVE_ORDERS",
)


def test_loads_safe_example_config() -> None:
    settings = load_settings(EXAMPLE_CONFIG)

    assert settings.app.name == "crypto-trading-assistant"
    assert settings.trading.live_trading is False
    assert settings.trading.dry_run is True
    assert settings.trading.require_confirm_before_order is True
    assert settings.agent_trading.enabled is False
    assert settings.agent_trading.allow_live_orders is False
    assert settings.agent_trading.max_autonomous_orders_per_day == 0
    assert settings.exchanges["mock"].enabled is True
    assert settings.exchanges["mock"].sandbox is True
    assert settings.strategy_runtime.demo_limit_price_buffer_pct == Decimal("0.001")
    assert settings.strategy_runtime.retrospective_enabled is True
    assert settings.strategy_runtime.retrospective_repeat_warning_threshold == 2
    assert settings.strategy_runtime.retrospective_demo_preflight_score_penalty == Decimal("30")
    assert settings.arbitrage.funding_min_annualized_pct == Decimal("20")
    assert settings.arbitrage.funding_max_basis_hedge_cost_pct == Decimal("0.30")
    assert settings.arbitrage.spot_perp_min_basis_pct == Decimal("0.05")
    assert settings.arbitrage.futures_basis_min_basis_pct == Decimal("0.05")
    assert settings.backtest.exchange == "mock"
    assert settings.backtest.symbol == "BTC/USDT"
    assert settings.backtest.strategy == "trend-breakout"
    assert settings.backtest.candles_limit == 120
    assert settings.backtest.slippage_pct == Decimal("0.0005")
    assert settings.backtest.walk_forward_windows == 2
    assert settings.backtest.min_window_trades == 1
    assert settings.universe.enabled is True
    assert settings.universe.min_24h_volume_usdt == Decimal("100000")
    assert settings.universe.max_spread_pct == Decimal("0.10")
    assert settings.universe.trend_return_threshold_pct == Decimal("100")
    assert settings.exit_optimization.take_profit_candidates_pct == [Decimal("1.00"), Decimal("2.00"), Decimal("3.00")]
    assert settings.exit_optimization.time_limit_candidates_bars == [8, 16, 32]
    assert settings.range_grid.enabled is True
    assert settings.range_grid.grid_levels == 6
    assert settings.range_grid.total_quote_usdt == Decimal("100")
    assert settings.smart_dca.enabled is True
    assert settings.smart_dca.exchange == "mock"
    assert settings.smart_dca.symbols == ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    assert settings.smart_dca.base_order_usdt == Decimal("25")
    assert settings.smart_dca.max_cycle_quote_usdt == Decimal("100")
    assert settings.smart_dca.min_drawdown_pct == Decimal("2")
    assert settings.smart_dca.target_weights_pct == {
        "BTC/USDT": Decimal("50"),
        "ETH/USDT": Decimal("30"),
        "SOL/USDT": Decimal("20"),
    }
    assert settings.smart_dca.rebalance_band_pct == Decimal("5")
    assert settings.smart_dca.min_depth_usdt == Decimal("1000")
    assert settings.hedged_maker.enabled is True
    assert settings.hedged_maker.maker_exchange == "mock"
    assert settings.hedged_maker.hedge_exchange == "mock_alt"
    assert settings.hedged_maker.paper_state_path == "logs/hedged-maker-paper-state.json"
    assert settings.hedged_maker.demo_state_path == "logs/hedged-maker-demo-state.json"
    assert settings.hedged_maker.min_edge_pct == Decimal("0.15")
    assert settings.hedged_maker.paper_queue_ahead_pct == Decimal("50")
    assert settings.hedged_maker.paper_min_fill_pct == Decimal("25")
    assert settings.hedged_maker.paper_stale_quote_seconds == 0
    assert settings.hedged_maker.paper_cancel_latency_seconds == 0
    assert settings.hedged_maker.paper_adverse_selection_buffer_pct == Decimal("0.05")
    assert settings.hedged_maker.paper_adverse_hedge_slippage_multiplier == Decimal("2")


def test_strategy_runtime_autopilot_defaults_are_safe() -> None:
    settings = load_settings(EXAMPLE_CONFIG)

    assert settings.strategy_runtime.autopilot_state_path == "logs/autopilot-state.json"
    assert settings.strategy_runtime.autopilot_default_interval_seconds == 60
    assert settings.strategy_runtime.autopilot_max_consecutive_blocked_cycles == 3
    assert settings.strategy_runtime.autopilot_stop_on_live_signal is True


def test_loads_separate_okx_demo_and_live_configs(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in OKX_CONFIG_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    demo = load_settings(OKX_DEMO_CONFIG)
    for name in OKX_CONFIG_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    live = load_settings(OKX_LIVE_CONFIG)

    assert demo.app.mode == "sandbox"
    assert demo.trading.live_trading is False
    assert demo.trading.dry_run is False
    assert demo.trading.require_confirm_before_order is False
    assert demo.exchanges["okx"].enabled is True
    assert demo.exchanges["okx"].sandbox is True
    assert demo.exchanges["okx"].okx_demo is True
    assert demo.agent_trading.enabled is True
    assert demo.agent_trading.allow_demo_orders is True
    assert demo.agent_trading.allow_live_orders is False
    assert demo.strategy_runtime.demo_limit_price_buffer_pct == Decimal("0.003")
    assert demo.strategy_runtime.demo_residual_inventory_tolerance_usdt == Decimal("0.10")
    assert demo.strategy_runtime.retrospective_path == "runtime/crypto/reports/28-strategy-retrospective.md"
    assert demo.strategy_runtime.retrospective_demo_preflight_score_penalty == Decimal("30")
    assert demo.arbitrage.funding_min_annualized_pct == Decimal("20")
    assert demo.arbitrage.funding_max_basis_hedge_cost_pct == Decimal("0.30")
    assert demo.universe.candles_limit == 120
    assert demo.exit_optimization.max_candidates == 5
    assert demo.range_grid.exchange == "okx"
    assert demo.range_grid.total_quote_usdt == Decimal("100")
    assert demo.smart_dca.exchange == "mock"
    assert demo.smart_dca.base_order_usdt == Decimal("25")
    assert demo.smart_dca.max_cycle_quote_usdt == Decimal("100")
    assert demo.hedged_maker.maker_exchange == "mock"
    assert demo.hedged_maker.hedge_exchange == "mock_alt"
    assert demo.hedged_maker.paper_state_path == "logs/hedged-maker-paper-state.json"
    assert demo.hedged_maker.demo_state_path == "logs/hedged-maker-demo-state.json"
    assert "hedged-maker" in demo.agent_trading.strategy_allowlist
    assert demo.hedged_maker.paper_queue_ahead_pct == Decimal("50")
    assert demo.hedged_maker.paper_min_fill_pct == Decimal("25")
    assert demo.hedged_maker.paper_stale_quote_seconds == 0
    assert demo.hedged_maker.paper_cancel_latency_seconds == 0
    assert demo.hedged_maker.paper_adverse_selection_buffer_pct == Decimal("0.05")
    assert demo.hedged_maker.paper_adverse_hedge_slippage_multiplier == Decimal("2")

    assert live.app.mode == "live"
    assert live.trading.live_trading is True
    assert live.trading.dry_run is False
    assert live.exchanges["okx"].enabled is True
    assert live.exchanges["okx"].sandbox is False
    assert live.exchanges["okx"].okx_demo is False
    assert live.agent_trading.enabled is True
    assert live.agent_trading.allow_demo_orders is False
    assert live.agent_trading.allow_live_orders is False
    assert live.strategy_runtime.retrospective_demo_preflight_score_penalty == Decimal("30")
    assert live.arbitrage.spot_perp_min_basis_pct == Decimal("0.05")
    for name in OKX_CONFIG_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_environment_override_updates_nested_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUANTBOT_CRYPTO_RISK_MAX_ORDER_VALUE_USDT", "42.5")
    monkeypatch.setenv("QUANTBOT_CRYPTO_ARBITRAGE_MIN_NET_PROFIT_PCT", "0.21")
    monkeypatch.setenv("QUANTBOT_CRYPTO_AGENT_MAX_ORDER_VALUE_USDT", "25")
    monkeypatch.setenv("QUANTBOT_CRYPTO_OKX_DEMO", "false")

    settings = load_settings(EXAMPLE_CONFIG)

    assert settings.risk.max_order_value_usdt == Decimal("42.5")
    assert settings.arbitrage.min_net_profit_pct == Decimal("0.21")
    assert settings.agent_trading.max_autonomous_order_value_usdt == Decimal("25")
    assert settings.exchanges["okx"].okx_demo is False


def test_config_specific_env_file_loads_okx_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("QUANTBOT_CRYPTO_OKX_API_KEY", "QUANTBOT_CRYPTO_OKX_API_SECRET", "QUANTBOT_CRYPTO_OKX_PASSPHRASE", "QUANTBOT_CRYPTO_OKX_DEMO"):
        monkeypatch.delenv(name, raising=False)
    env_path = tmp_path / ".env.okx.demo"
    env_path.write_text(
        "\n".join(
            [
                "QUANTBOT_CRYPTO_OKX_API_KEY=demo-key",
                "QUANTBOT_CRYPTO_OKX_API_SECRET=demo-secret",
                "QUANTBOT_CRYPTO_OKX_PASSPHRASE=demo-passphrase",
                "QUANTBOT_CRYPTO_OKX_DEMO=true",
            ]
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "okx-demo.yaml"
    config_path.write_text(
        f"""
app:
  env_file: {env_path}
exchanges:
  okx:
    enabled: true
    sandbox: true
    adapter: okx
    okx_demo: true
    api_key_env: QUANTBOT_CRYPTO_OKX_API_KEY
    api_secret_env: QUANTBOT_CRYPTO_OKX_API_SECRET
    passphrase_env: QUANTBOT_CRYPTO_OKX_PASSPHRASE
""".strip(),
        encoding="utf-8",
    )

    settings = load_settings(config_path)
    redacted = settings.redacted_dict()

    assert settings.exchanges["okx"].okx_demo is True
    assert redacted["exchanges"]["okx"]["credentials"]["api_key"] == "***redacted***"
    assert "demo-secret" not in str(redacted)


def test_redacted_dump_never_exposes_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUANTBOT_CRYPTO_MOCK_API_KEY", "secret-key")
    monkeypatch.setenv("QUANTBOT_CRYPTO_MOCK_API_SECRET", "secret-value")

    settings = load_settings(EXAMPLE_CONFIG)
    redacted = settings.redacted_dict()

    rendered = str(redacted)
    assert "secret-key" not in rendered
    assert "secret-value" not in rendered
    assert redacted["exchanges"]["mock"]["credentials"]["api_key"] == "***redacted***"
    assert redacted["exchanges"]["mock"]["credentials"]["api_secret"] == "***redacted***"


def test_rejects_conflicting_live_and_dry_run(tmp_path: Path) -> None:
    config_path = tmp_path / "bad.yaml"
    config_path.write_text(
        """
app:
  name: bad
trading:
  live_trading: true
  dry_run: true
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="live_trading=true requires dry_run=false"):
        load_settings(config_path)


def test_rejects_okx_sandbox_demo_mismatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("QUANTBOT_CRYPTO_OKX_DEMO", raising=False)
    config_path = tmp_path / "bad-okx.yaml"
    config_path.write_text(
        """
exchanges:
  okx:
    enabled: true
    sandbox: true
    adapter: okx
    okx_demo: false
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="okx_demo must match sandbox"):
        load_settings(config_path)

"""Tests for OKX Demo Trading hedged-maker order management."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from quantbot.crypto.runtime.arbitrage.opportunity import ArbitrageOpportunity
from quantbot.crypto.runtime.config.schema import AgentTradingConfig, ExchangeConfig, HedgedMakerConfig, Settings, TradingConfig
from quantbot.crypto.runtime.exchanges.base import Exchange
from quantbot.crypto.runtime.exchanges.factory import ExchangeFactory
from quantbot.crypto.runtime.exchanges.mock import MockExchange
from quantbot.crypto.runtime.exceptions import SafetyError
from quantbot.crypto.runtime.strategies.hedged_maker_candidate import HedgedMakerDemoCandidateService
from quantbot.crypto.runtime.strategies.hedged_maker_demo import HedgedMakerDemoOrderManager
from quantbot.crypto.runtime.strategies.platform import StrategyController


def test_hedged_maker_demo_manager_blocks_default_dry_run_settings(tmp_path: Path) -> None:
    settings = Settings(
        hedged_maker=HedgedMakerConfig(demo_state_path=str(tmp_path / "hm-demo-state.json")),
    )

    with pytest.raises(SafetyError) as exc:
        HedgedMakerDemoOrderManager(settings, provider=FakeHedgedMakerDemoProvider()).manage(_opportunity())

    assert "dry_run_enabled" in str(exc.value)


def test_hedged_maker_demo_manager_submits_post_only_maker_quote(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_demo_env(monkeypatch)
    settings = _demo_settings(tmp_path)
    provider = FakeHedgedMakerDemoProvider()

    result = HedgedMakerDemoOrderManager(settings, provider=provider).manage(
        _opportunity(),
        now=datetime(2026, 5, 16, 1, 0, tzinfo=UTC),
    )

    assert result.status == "maker_submitted"
    assert result.demo_orders_sent is True
    assert result.live_orders_sent is False
    assert result.maker_order is not None
    assert result.maker_order["status"] == "open"
    assert provider.trade_posts[0][1]["ordType"] == "post_only"
    assert provider.trade_posts[0][1]["side"] == "buy"
    assert provider.trade_posts[0][1]["tdMode"] == "cash"
    state = json.loads(Path(settings.hedged_maker.demo_state_path).read_text(encoding="utf-8"))
    assert state["orders"][0]["order_id"] == result.maker_order["order_id"]
    audit = Path(settings.agent_trading.audit_log_path).read_text(encoding="utf-8")
    assert "hedged_maker_demo_manager" in audit
    assert "demo-secret" not in audit


def test_hedged_maker_demo_manager_hedges_only_new_maker_fill(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_demo_env(monkeypatch)
    settings = _demo_settings(tmp_path)
    provider = FakeHedgedMakerDemoProvider()
    _write_state(
        settings,
        [
            {
                "order_id": "maker-1",
                "opportunity_id": "hm-okx-demo",
                "symbol": "BTC/USDT",
                "maker_side": "buy",
                "hedge_side": "sell",
                "price": "79990",
                "quantity": "0.001",
                "filled_quantity": "0",
                "remaining_quantity": "0.001",
                "status": "open",
                "created_at": "2026-05-16T00:00:00+00:00",
                "updated_at": "2026-05-16T00:00:00+00:00",
            }
        ],
    )
    provider.order_details["maker-1"] = _detail("maker-1", state="filled", side="buy", fill_size="0.001")

    result = HedgedMakerDemoOrderManager(settings, provider=provider).manage(
        _opportunity(),
        now=datetime(2026, 5, 16, 1, 0, tzinfo=UTC),
    )

    assert result.status == "maker_filled_hedged"
    assert result.hedge_order is not None
    assert result.hedge_order["side"] == "sell"
    assert result.hedge_order["quantity"] == "0.001"
    hedge_post = provider.trade_posts[-1][1]
    assert hedge_post["ordType"] == "limit"
    assert hedge_post["side"] == "sell"
    assert hedge_post["sz"] == "0.001"
    state = json.loads(Path(settings.hedged_maker.demo_state_path).read_text(encoding="utf-8"))
    assert state["orders"][0]["status"] == "hedged"
    assert state["orders"][0]["hedge_order_id"] == result.hedge_order["order_id"]


def test_hedged_maker_demo_manager_replaces_stale_open_quote(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_demo_env(monkeypatch)
    settings = _demo_settings(tmp_path)
    provider = FakeHedgedMakerDemoProvider()
    created_at = datetime(2026, 5, 16, 0, 0, tzinfo=UTC)
    _write_state(
        settings,
        [
            {
                "order_id": "maker-old",
                "opportunity_id": "hm-okx-demo",
                "symbol": "BTC/USDT",
                "maker_side": "buy",
                "hedge_side": "sell",
                "price": "79900",
                "quantity": "0.001",
                "filled_quantity": "0",
                "remaining_quantity": "0.001",
                "status": "open",
                "created_at": created_at.isoformat(),
                "updated_at": created_at.isoformat(),
            }
        ],
    )
    provider.order_details["maker-old"] = _detail("maker-old", state="live", side="buy", fill_size="0")

    result = HedgedMakerDemoOrderManager(settings, provider=provider).manage(
        _opportunity(),
        now=created_at + timedelta(seconds=settings.strategy_runtime.order_ttl_seconds + 1),
    )

    assert result.status == "maker_replaced"
    assert result.canceled_order_ids == ["maker-old"]
    assert provider.canceled_order_ids == ["maker-old"]
    assert provider.trade_posts[-1][1]["ordType"] == "post_only"
    state = json.loads(Path(settings.hedged_maker.demo_state_path).read_text(encoding="utf-8"))
    assert state["orders"][0]["status"] == "canceled"
    assert state["orders"][1]["status"] == "open"


def test_hedged_maker_demo_candidate_builds_okx_opportunity_payload(tmp_path: Path) -> None:
    settings = _demo_candidate_settings(tmp_path)

    report = HedgedMakerDemoCandidateService(settings, OkxMockFactory(settings)).candidate(
        symbol="BTC/USDT",
        target_exchange="okx",
    )
    payload = report.to_dict()

    assert payload["read_only"] is True
    assert payload["orders_sent"] is False
    assert payload["live_orders_sent"] is False
    assert payload["approved"] is True
    assert payload["demo_manager_compatible"] is True
    opportunity = payload["opportunity_file_payload"]
    assert opportunity["strategy_type"] == "hedged-maker"
    assert opportunity["buy_exchange"] == "okx"
    assert opportunity["sell_exchange"] == "okx"
    assert opportunity["metadata"]["maker_quote"]["exchange"] == "okx"
    assert opportunity["metadata"]["hedge_preview"]["exchange"] == "okx"
    assert opportunity["metadata"]["execution_quality"]["spread_persistence"]["passed"] is True
    assert opportunity["metadata"]["execution_quality"]["depth_fill"]["buy"]["complete"] is True
    assert opportunity["metadata"]["execution_quality"]["depth_fill"]["sell"]["complete"] is True
    assert "submit_with_strategy_hedged_maker_demo_after_demo_gate" in payload["next_actions"]


def test_strategy_scan_uses_okx_hedged_maker_candidate_diagnostics(tmp_path: Path) -> None:
    settings = _demo_candidate_settings(tmp_path)
    settings.exchanges["mock_alt"] = ExchangeConfig(enabled=False, sandbox=True, adapter="mock")
    settings.hedged_maker.hedge_exchange = "mock_alt"

    report = StrategyController(settings, OkxMockFactory(settings)).scan(
        strategy_name="hedged-maker",
        symbol="BTC/USDT",
    )[0]

    assert report.opportunities == []
    assert report.diagnostics["read_only"] is True
    assert report.diagnostics["demo_manager_compatible"] is True
    assert "diagnostic_error:Exchange is disabled: mock_alt" not in report.diagnostics["reasons"]
    opportunity = report.diagnostics["opportunity_file_payload"]
    assert opportunity["buy_exchange"] == "okx"
    assert opportunity["sell_exchange"] == "okx"
    assert opportunity["metadata"]["maker_quote"]["exchange"] == "okx"
    assert opportunity["metadata"]["hedge_preview"]["exchange"] == "okx"


def test_strategy_scan_honors_explicit_okx_exchange_for_hedged_maker_diagnostics(tmp_path: Path) -> None:
    settings = _demo_candidate_settings(tmp_path)
    okx_config = settings.exchanges["okx"]
    settings.exchanges = {
        "mock": ExchangeConfig(enabled=True, sandbox=True, adapter="mock"),
        "okx": okx_config,
        "mock_alt": ExchangeConfig(enabled=False, sandbox=True, adapter="mock"),
    }
    settings.hedged_maker.hedge_exchange = "mock_alt"

    report = StrategyController(settings, OkxMockFactory(settings)).scan(
        strategy_name="hedged-maker",
        symbol="BTC/USDT",
        exchange="okx",
    )[0]

    assert report.opportunities == []
    assert report.diagnostics["target_exchange"] == "okx"
    assert report.diagnostics["demo_manager_compatible"] is True
    assert "diagnostic_error:Exchange is disabled: mock_alt" not in report.diagnostics["reasons"]
    opportunity = report.diagnostics["opportunity_file_payload"]
    assert opportunity["buy_exchange"] == "okx"
    assert opportunity["sell_exchange"] == "okx"


def _demo_settings(tmp_path: Path) -> Settings:
    return Settings(
        trading=TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False),
        exchanges={
            "okx": ExchangeConfig(
                enabled=True,
                sandbox=True,
                adapter="okx",
                okx_demo=True,
                api_key_env="QUANTBOT_CRYPTO_OKX_API_KEY",
                api_secret_env="QUANTBOT_CRYPTO_OKX_API_SECRET",
                passphrase_env="QUANTBOT_CRYPTO_OKX_PASSPHRASE",
            )
        },
        agent_trading=AgentTradingConfig(
            enabled=True,
            allow_demo_orders=True,
            allow_live_orders=False,
            strategy_allowlist=["hedged-maker"],
            allowed_exchanges=["okx"],
            max_autonomous_order_value_usdt=Decimal("100"),
            max_autonomous_orders_per_day=5,
            audit_log_path=str(tmp_path / "agent-demo-audit.jsonl"),
            policy_id="test-demo-policy",
        ),
        hedged_maker=HedgedMakerConfig(
            demo_state_path=str(tmp_path / "hm-demo-state.json"),
            quote_notional_usdt=Decimal("80"),
        ),
    )


def _demo_candidate_settings(tmp_path: Path) -> Settings:
    return Settings(
        trading=TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False),
        exchanges={
            "okx": ExchangeConfig(
                enabled=True,
                sandbox=True,
                adapter="okx",
                okx_demo=True,
                api_key_env="QUANTBOT_CRYPTO_OKX_API_KEY",
                api_secret_env="QUANTBOT_CRYPTO_OKX_API_SECRET",
                passphrase_env="QUANTBOT_CRYPTO_OKX_PASSPHRASE",
            )
        },
        agent_trading=AgentTradingConfig(
            enabled=True,
            allow_demo_orders=True,
            allow_live_orders=False,
            strategy_allowlist=["hedged-maker"],
            allowed_exchanges=["okx"],
            max_autonomous_order_value_usdt=Decimal("100"),
            max_autonomous_orders_per_day=5,
            audit_log_path=str(tmp_path / "agent-demo-audit.jsonl"),
            policy_id="test-demo-policy",
        ),
        hedged_maker=HedgedMakerConfig(
            quote_spread_pct=Decimal("0.50"),
            min_edge_pct=Decimal("0.01"),
            quote_notional_usdt=Decimal("80"),
            demo_state_path=str(tmp_path / "hm-demo-state.json"),
        ),
    )


def _opportunity() -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        opportunity_id="hm-okx-demo",
        strategy_type="hedged-maker",
        symbol="BTC/USDT",
        buy_exchange="okx",
        sell_exchange="okx",
        expected_profit=Decimal("1.20"),
        expected_profit_pct=Decimal("1.50"),
        estimated_fee=Decimal("0.10"),
        estimated_slippage=Decimal("0.01"),
        required_capital=Decimal("80"),
        net_profit=Decimal("1.09"),
        risk_score=Decimal("0.20"),
        confidence=Decimal("0.80"),
        metadata={
            "maker_quote": {
                "exchange": "okx",
                "symbol": "BTC/USDT",
                "side": "buy",
                "price": "79990",
                "quantity": "0.001",
                "notional_usdt": "79.99",
            },
            "hedge_preview": {
                "exchange": "okx",
                "symbol": "BTC/USDT",
                "side": "sell",
                "price": "79980",
                "quantity": "0.001",
                "notional_usdt": "79.98",
            },
            "execution_quality": {
                "spread_persistence": {"passed": True},
                "depth_fill": {
                    "buy": {"complete": True},
                    "sell": {"complete": True},
                },
            },
        },
    )


def _set_demo_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUANTBOT_CRYPTO_OKX_API_KEY", "demo-key")
    monkeypatch.setenv("QUANTBOT_CRYPTO_OKX_API_SECRET", "demo-secret")
    monkeypatch.setenv("QUANTBOT_CRYPTO_OKX_PASSPHRASE", "demo-passphrase")
    monkeypatch.setenv("QUANTBOT_CRYPTO_AGENT_OPERATOR_ID", "operator-1")
    monkeypatch.delenv("QUANTBOT_CRYPTO_AGENT_LIVE_KILL_SWITCH", raising=False)


def _write_state(settings: Settings, orders: list[dict[str, Any]]) -> None:
    path = Path(settings.hedged_maker.demo_state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"orders": orders}, ensure_ascii=False), encoding="utf-8")


def _detail(order_id: str, *, state: str, side: str, fill_size: str) -> dict[str, str]:
    return {
        "ordId": order_id,
        "instId": "BTC-USDT",
        "side": side,
        "ordType": "post_only",
        "state": state,
        "sz": "0.001",
        "fillSz": fill_size,
        "accFillSz": fill_size,
        "px": "79990",
        "avgPx": "79990" if Decimal(fill_size) > 0 else "0",
        "cTime": "1",
        "uTime": "2",
    }


class OkxMockFactory(ExchangeFactory):
    """Exchange factory that avoids network while preserving OKX config semantics."""

    def get(self, name: str) -> Exchange:
        if name == "okx":
            return MockExchange(name="mock")
        return super().get(name)


class FakeHedgedMakerDemoProvider:
    """Fake OKX Demo provider for hedged-maker manager tests."""

    configured = True
    demo = True

    def __init__(self) -> None:
        self.counter = 0
        self.trade_posts: list[tuple[str, dict[str, str]]] = []
        self.order_details: dict[str, dict[str, str]] = {}
        self.canceled_order_ids: list[str] = []

    def get_price(self, symbol: str) -> float:
        return 80000.0

    def _get(self, path: str, params: dict[str, str] | None = None, *, signed: bool = False) -> dict[str, Any]:
        if path == "/api/v5/account/config":
            return {"code": "0", "data": [{"acctLv": "1"}]}
        if path == "/api/v5/trade/order":
            order_id = str((params or {})["ordId"])
            return {"code": "0", "data": [self.order_details[order_id]]}
        if path == "/api/v5/market/books":
            return {
                "code": "0",
                "data": [
                    {
                        "bids": [["79980", "2"]],
                        "asks": [["80020", "2"]],
                    }
                ],
            }
        raise AssertionError(f"unexpected _get path: {path}")

    def _post(self, path: str, body: dict[str, str]) -> dict[str, Any]:
        if path != "/api/v5/trade/order":
            raise AssertionError(f"unexpected _post path: {path}")
        self.counter += 1
        prefix = "maker" if body["ordType"] == "post_only" else "hedge"
        order_id = f"{prefix}-{self.counter}"
        state = "live" if body["ordType"] == "post_only" else "filled"
        fill_size = "0" if body["ordType"] == "post_only" else body["sz"]
        self.trade_posts.append((path, dict(body)))
        self.order_details[order_id] = {
            "ordId": order_id,
            "instId": body["instId"],
            "side": body["side"],
            "ordType": body["ordType"],
            "state": state,
            "sz": body["sz"],
            "fillSz": fill_size,
            "accFillSz": fill_size,
            "px": body["px"],
            "avgPx": body["px"] if fill_size != "0" else "0",
            "cTime": "1",
            "uTime": "2",
        }
        return {"code": "0", "data": [{"ordId": order_id}]}

    def cancel_order(self, symbol: str, order_id: str) -> object:
        self.canceled_order_ids.append(order_id)
        detail = self.order_details.setdefault(order_id, _detail(order_id, state="canceled", side="buy", fill_size="0"))
        detail["state"] = "canceled"
        return object()

"""Tests for the read-only DEX/CLMM LP readiness gate."""

from __future__ import annotations

from pathlib import Path

from quantbot.crypto.runtime.config.loader import load_settings
from quantbot.crypto.runtime.strategies.dex_readiness import DexLpReadinessService


ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_CONFIG = ROOT / "config" / "crypto" / "config.example.yaml"


def test_dex_lp_readiness_blocks_by_default() -> None:
    settings = load_settings(EXAMPLE_CONFIG)

    report = DexLpReadinessService(settings).report()

    assert report.read_only is True
    assert report.orders_sent is False
    assert report.live_orders_sent is False
    assert report.status == "Deferred"
    assert report.testnet_ready is False
    assert report.execution_supported is False
    assert "dex_lp_disabled" in report.reasons
    assert "dex_gateway_missing" in report.reasons
    assert "wallet_policy_missing" in report.reasons
    assert "live_orders_unsupported" in report.reasons


def test_dex_lp_readiness_can_reach_testnet_ready_without_execution_support(tmp_path: Path) -> None:
    evidence_path = tmp_path / "testnet-evidence.md"
    evidence_path.write_text("# DEX testnet evidence\n", encoding="utf-8")
    settings = load_settings(EXAMPLE_CONFIG)
    settings.dex_lp.enabled = True
    settings.dex_lp.gateway_enabled = True
    settings.dex_lp.gateway_url = "http://127.0.0.1:15888"
    settings.dex_lp.network = "testnet"
    settings.dex_lp.wallet_policy_ack = True
    settings.dex_lp.gas_model_enabled = True
    settings.dex_lp.mev_protection_enabled = True
    settings.dex_lp.testnet_validation_evidence_path = str(evidence_path)

    report = DexLpReadinessService(settings).report()

    assert report.status == "Testnet Ready"
    assert report.testnet_ready is True
    assert report.execution_supported is False
    assert report.live_orders_sent is False
    assert report.reasons == ["execution_adapter_not_implemented", "live_orders_unsupported"]

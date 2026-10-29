"""配置结构、范围和稳定指纹测试。"""

from __future__ import annotations

import copy

import pytest
import yaml

from quantbot.price_action.config import (
    ConfigValidationError,
    config_hash,
    load_config,
    validate_config,
)

from .conftest import make_cfg


def test_config_hash_is_independent_of_mapping_order():
    left = {"b": {"z": 2, "a": 1}, "a": [3, 4]}
    right = {"a": [3, 4], "b": {"a": 1, "z": 2}}

    assert config_hash(left) == config_hash(right)
    right["b"]["z"] = 3
    assert config_hash(left) != config_hash(right)


def test_current_and_fixture_configs_satisfy_schema():
    assert load_config()["strategy"]["breakout_window"] == 55
    cfg = make_cfg()
    cfg["review"] = {
        "journal_path": "logs/trading_journal.sqlite",
        "enabled": True,
        "candidate_log_limit": 100,
        "paper_min_trading_days": 60,
    }
    cfg["governance"] = {"artifact_dir": "artifacts/governance"}
    assert validate_config(cfg) is cfg


def test_validation_rejects_unknown_fields_and_bad_ranges_together():
    cfg = make_cfg()
    cfg["strategy"]["atr_widnow"] = 14
    cfg["risk"]["risk_per_trade"] = 0

    with pytest.raises(ConfigValidationError) as exc_info:
        validate_config(cfg)

    assert "strategy 含未知字段: atr_widnow" in str(exc_info.value)
    assert "risk.risk_per_trade 必须位于 (0, 1]" in str(exc_info.value)


def test_load_config_validates_yaml_boundary(tmp_path):
    cfg = copy.deepcopy(make_cfg())
    cfg["backtest"]["start"] = "not-a-date"
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    with pytest.raises(ConfigValidationError, match="backtest.start"):
        load_config(path)


def test_validation_rejects_reversed_date_window():
    cfg = make_cfg()
    cfg["backtest"]["start"] = "2031-01-01"

    with pytest.raises(ConfigValidationError, match="start 不得晚于"):
        validate_config(cfg)


def test_production_load_applies_price_action_policy(tmp_path):
    cfg = copy.deepcopy(make_cfg())
    cfg["strategy"]["earnings_avoid_days"] = 3
    path = tmp_path / "research-only.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    # 研究配置可以被加载，但不能伪装成生产配置。
    assert load_config(path)["strategy"]["earnings_avoid_days"] == 3
    with pytest.raises(ValueError, match="earnings_avoid_days"):
        load_config(path, production=True)

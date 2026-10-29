"""配置加载、校验与稳定指纹。

项目仍以普通 ``dict`` 作为运行时配置，避免让策略、回测和实盘依赖某个
配置框架。本模块只在边界处做三件事：拒绝拼写错误和明显越界的数值、
提供与字典键顺序无关的稳定哈希、按需启用生产价格行为政策。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

# 项目根目录（本文件位于 quantbot.price_action/ 之下）
ROOT_DIR = Path(__file__).resolve().parents[3]

DEFAULT_CONFIG_PATH = ROOT_DIR / "config" / "price-action.legacy.yaml"


class ConfigValidationError(ValueError):
    """配置结构或取值不满足运行契约。"""

    def __init__(self, errors: list[str] | tuple[str, ...]):
        self.errors = tuple(errors)
        super().__init__("配置校验失败：\n- " + "\n- ".join(self.errors))


# 这里刻意只列运行时真正读取的字段。未知字段通常是拼写错误，应尽早失败；
# 增加新配置时同步扩展这张表，即可让研究、回测和生产共用同一契约。
_SECTION_FIELDS: dict[str, frozenset[str]] = {
    "data": frozenset({"cache_dir", "start", "end", "snapshot"}),
    "universe": frozenset({"mode", "top_adv"}),
    "backtest": frozenset({
        "start", "end", "initial_capital", "slippage_bps",
        "commission_per_share", "cash_yield", "use_prefilter",
    }),
    "strategy": frozenset({
        "breakout_window", "exit_window", "atr_window", "atr_stop_mult",
        "atr_trail_mult", "swing_k", "momentum_window", "momentum_filter",
        "rank_by", "pullback_depth_atr", "earnings_avoid_days",
        "use_inside_bar", "use_weekly_confirm", "time_stop_days",
        "time_stop_min_r", "pyramid_max_adds", "pyramid_trigger_r",
        "pyramid_size_frac",
    }),
    "risk": frozenset({
        "risk_per_trade", "max_positions", "max_weight", "max_per_sector",
        "dedup_share_classes",
    }),
    "shorts": frozenset({"enabled", "max_positions", "borrow_rate"}),
    "base_position": frozenset({"enabled", "symbol", "min_level", "cash_buffer"}),
    "leverage": frozenset({
        "enabled", "target_vol", "vol_window", "max_leverage",
        "min_leverage", "margin_spread",
    }),
    "regime": frozenset({
        "symbol", "momentum_window", "recovery_window", "structural_reopen",
        "reopen_window",
    }),
    "broker": frozenset({"provider", "paper"}),
    "live": frozenset({"state_file", "cache_dir", "reconcile_max_cache_age_days"}),
    "review": frozenset({
        "journal_path", "enabled", "candidate_log_limit", "paper_min_trading_days",
    }),
    "governance": frozenset({"artifact_dir"}),
}

_REQUIRED_SECTIONS = frozenset({"data", "backtest", "strategy", "risk", "regime", "live"})
_REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "data": frozenset({"cache_dir", "start", "end"}),
    "backtest": frozenset({
        "start", "end", "initial_capital", "slippage_bps", "commission_per_share",
    }),
    "strategy": frozenset({
        "breakout_window", "exit_window", "atr_window", "atr_stop_mult",
        "atr_trail_mult", "swing_k", "momentum_window", "pullback_depth_atr",
    }),
    "risk": frozenset({"risk_per_trade", "max_positions", "max_weight"}),
    "regime": frozenset({"symbol", "momentum_window"}),
    "live": frozenset({"state_file"}),
}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(float(value))


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _positive_number(value: Any) -> bool:
    return _is_number(value) and float(value) > 0


def _nonnegative_number(value: Any) -> bool:
    return _is_number(value) and float(value) >= 0


def _ratio_open_closed(value: Any) -> bool:
    return _is_number(value) and 0 < float(value) <= 1


def _ratio_closed(value: Any) -> bool:
    return _is_number(value) and 0 <= float(value) <= 1


def _boolean(value: Any) -> bool:
    return isinstance(value, bool)


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _optional_nonempty_string(value: Any) -> bool:
    return value is None or _nonempty_string(value)


_FIELD_RULES: dict[tuple[str, str], tuple[Callable[[Any], bool], str]] = {
    ("data", "cache_dir"): (_nonempty_string, "必须是非空字符串"),
    ("data", "snapshot"): (_optional_nonempty_string, "必须为 null 或非空字符串"),
    ("backtest", "initial_capital"): (_positive_number, "必须大于 0"),
    ("backtest", "slippage_bps"): (_nonnegative_number, "必须大于等于 0"),
    ("backtest", "commission_per_share"): (_nonnegative_number, "必须大于等于 0"),
    ("backtest", "cash_yield"): (_boolean, "必须是布尔值"),
    ("backtest", "use_prefilter"): (_boolean, "必须是布尔值"),
    ("universe", "top_adv"): (_nonnegative_int, "必须是非负整数"),
    ("strategy", "breakout_window"): (_positive_int, "必须是正整数"),
    ("strategy", "exit_window"): (_positive_int, "必须是正整数"),
    ("strategy", "atr_window"): (_positive_int, "必须是正整数"),
    ("strategy", "atr_stop_mult"): (_positive_number, "必须大于 0"),
    ("strategy", "atr_trail_mult"): (_positive_number, "必须大于 0"),
    ("strategy", "swing_k"): (_positive_int, "必须是正整数"),
    ("strategy", "momentum_window"): (_positive_int, "必须是正整数"),
    ("strategy", "pullback_depth_atr"): (_nonnegative_number, "必须大于等于 0"),
    ("strategy", "earnings_avoid_days"): (_nonnegative_int, "必须是非负整数"),
    ("strategy", "use_inside_bar"): (_boolean, "必须是布尔值"),
    ("strategy", "use_weekly_confirm"): (_boolean, "必须是布尔值"),
    ("strategy", "time_stop_days"): (_nonnegative_int, "必须是非负整数"),
    ("strategy", "time_stop_min_r"): (_nonnegative_number, "必须大于等于 0"),
    ("strategy", "pyramid_max_adds"): (_nonnegative_int, "必须是非负整数"),
    ("strategy", "pyramid_trigger_r"): (_positive_number, "必须大于 0"),
    ("strategy", "pyramid_size_frac"): (_ratio_open_closed, "必须位于 (0, 1]"),
    ("risk", "risk_per_trade"): (_ratio_open_closed, "必须位于 (0, 1]"),
    ("risk", "max_positions"): (_positive_int, "必须是正整数"),
    ("risk", "max_weight"): (_ratio_open_closed, "必须位于 (0, 1]"),
    ("risk", "max_per_sector"): (_nonnegative_int, "必须是非负整数"),
    ("risk", "dedup_share_classes"): (_boolean, "必须是布尔值"),
    ("shorts", "enabled"): (_boolean, "必须是布尔值"),
    ("shorts", "max_positions"): (_positive_int, "必须是正整数"),
    ("shorts", "borrow_rate"): (_nonnegative_number, "必须大于等于 0"),
    ("base_position", "enabled"): (_boolean, "必须是布尔值"),
    ("base_position", "symbol"): (_nonempty_string, "必须是非空字符串"),
    ("base_position", "min_level"): (_nonnegative_int, "必须是非负整数"),
    ("base_position", "cash_buffer"): (_ratio_closed, "必须位于 [0, 1]"),
    ("leverage", "enabled"): (_boolean, "必须是布尔值"),
    ("leverage", "target_vol"): (_positive_number, "必须大于 0"),
    ("leverage", "vol_window"): (_positive_int, "必须是正整数"),
    ("leverage", "max_leverage"): (_positive_number, "必须大于 0"),
    ("leverage", "min_leverage"): (_positive_number, "必须大于 0"),
    ("leverage", "margin_spread"): (_nonnegative_number, "必须大于等于 0"),
    ("regime", "symbol"): (_nonempty_string, "必须是非空字符串"),
    ("regime", "momentum_window"): (_positive_int, "必须是正整数"),
    ("regime", "reopen_window"): (_positive_int, "必须是正整数"),
    ("regime", "structural_reopen"): (_boolean, "必须是布尔值"),
    ("broker", "provider"): (_nonempty_string, "必须是非空字符串"),
    ("broker", "paper"): (_boolean, "必须是布尔值"),
    ("live", "state_file"): (_nonempty_string, "必须是非空字符串"),
    ("live", "cache_dir"): (_nonempty_string, "必须是非空字符串"),
    ("live", "reconcile_max_cache_age_days"): (
        _nonnegative_int, "必须是非负整数",
    ),
    ("review", "journal_path"): (_nonempty_string, "必须是非空字符串"),
    ("review", "enabled"): (_boolean, "必须是布尔值"),
    ("review", "candidate_log_limit"): (_positive_int, "必须是正整数"),
    ("review", "paper_min_trading_days"): (_nonnegative_int, "必须是非负整数"),
    ("governance", "artifact_dir"): (_nonempty_string, "必须是非空字符串"),
}


def _validate_iso_date(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, str):
        errors.append(f"{path} 必须是 YYYY-MM-DD 字符串")
        return
    try:
        date.fromisoformat(value)
    except ValueError:
        errors.append(f"{path} 必须是有效的 YYYY-MM-DD 日期")


def validate_config(cfg: Mapping[str, Any]) -> Mapping[str, Any]:
    """校验配置结构、未知字段和关键范围，成功时原样返回 ``cfg``。

    校验是刻意适度的：它覆盖会导致错误交易或静默回退的字段，但不把
    每个研究参数之间的经验关系固化成规则。生产宗旨由
    :func:`quantbot.price_action.governance.validate_production_config` 另行校验。
    """
    if not isinstance(cfg, Mapping):
        raise ConfigValidationError(["顶层必须是映射"])

    errors: list[str] = []
    unknown_sections = sorted(set(cfg) - set(_SECTION_FIELDS), key=str)
    if unknown_sections:
        errors.append(f"未知顶层字段: {', '.join(map(str, unknown_sections))}")

    missing_sections = sorted(_REQUIRED_SECTIONS - set(cfg))
    if missing_sections:
        errors.append(f"缺少必需小节: {', '.join(missing_sections)}")

    for section, value in cfg.items():
        if section not in _SECTION_FIELDS:
            continue
        if not isinstance(value, Mapping):
            errors.append(f"{section} 必须是映射")
            continue
        unknown = sorted(set(value) - set(_SECTION_FIELDS[section]), key=str)
        if unknown:
            errors.append(f"{section} 含未知字段: {', '.join(map(str, unknown))}")
        missing = sorted(_REQUIRED_FIELDS.get(section, frozenset()) - set(value))
        if missing:
            errors.append(f"{section} 缺少必需字段: {', '.join(missing)}")
        for field, field_value in value.items():
            rule = _FIELD_RULES.get((section, field))
            if rule is not None and not rule[0](field_value):
                errors.append(f"{section}.{field} {rule[1]}")

    for section, field in (("data", "start"), ("data", "end"),
                           ("backtest", "start"), ("backtest", "end")):
        section_value = cfg.get(section)
        if isinstance(section_value, Mapping) and field in section_value:
            _validate_iso_date(section_value[field], f"{section}.{field}", errors)
    for section in ("data", "backtest"):
        section_value = cfg.get(section)
        if not isinstance(section_value, Mapping):
            continue
        start, end = section_value.get("start"), section_value.get("end")
        if isinstance(start, str) and isinstance(end, str):
            try:
                if date.fromisoformat(start) > date.fromisoformat(end):
                    errors.append(f"{section}.start 不得晚于 {section}.end")
            except ValueError:
                pass  # 单字段日期错误已由上面的校验报告

    universe = cfg.get("universe")
    if isinstance(universe, Mapping):
        mode = universe.get("mode", "static")
        if not isinstance(mode, str) or mode not in {"static", "sp500_pit"}:
            errors.append("universe.mode 必须是 static 或 sp500_pit")

    strategy = cfg.get("strategy")
    if isinstance(strategy, Mapping):
        momentum_filter = strategy.get("momentum_filter", "absolute")
        if not isinstance(momentum_filter, str) \
                or momentum_filter not in {"absolute", "relative"}:
            errors.append("strategy.momentum_filter 必须是 absolute 或 relative")
        rank_by = strategy.get("rank_by", "momentum")
        if not isinstance(rank_by, str) or rank_by not in {"momentum", "sharpe_momentum"}:
            errors.append("strategy.rank_by 必须是 momentum 或 sharpe_momentum")

    regime = cfg.get("regime")
    if isinstance(regime, Mapping):
        recovery = regime.get("recovery_window")
        if recovery is not None and not _positive_int(recovery):
            errors.append("regime.recovery_window 必须为 null 或正整数")

    leverage = cfg.get("leverage")
    if isinstance(leverage, Mapping):
        min_lev, max_lev = leverage.get("min_leverage"), leverage.get("max_leverage")
        if _positive_number(min_lev) and _positive_number(max_lev) \
                and float(min_lev) > float(max_lev):
            errors.append("leverage.min_leverage 不得大于 max_leverage")

    if errors:
        raise ConfigValidationError(errors)
    return cfg


def _canonical_value(value: Any) -> Any:
    """转换为可确定性 JSON 编码的值；不改变映射和序列的语义。"""
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("配置键必须是字符串")
        return {key: _canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("配置不能包含 NaN 或无穷大")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"配置包含不可序列化类型: {type(value).__name__}")


def canonical_config_json(cfg: Mapping[str, Any]) -> str:
    """返回与字典插入顺序和空白无关的规范 JSON。"""
    return json.dumps(
        _canonical_value(cfg), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )


def config_hash(cfg: Mapping[str, Any]) -> str:
    """返回配置规范表示的 SHA-256 十六进制指纹。"""
    payload = canonical_config_json(cfg).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_config(
    path: str | Path | None = None,
    *,
    production: bool = False,
) -> dict[str, Any]:
    """加载并校验 YAML 配置。

    ``production=True`` 会额外应用价格行为生产政策；默认关闭以允许历史
    研究代码显式测试被证伪模块，但正常配置仍始终执行结构和范围校验。
    """
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    validate_config(cfg)
    if production:
        # 延迟导入避免治理契约反向依赖配置模块时形成导入环。
        from .governance.policy import validate_production_config

        validate_production_config(cfg)
    return dict(cfg)


def get_alpaca_keys() -> tuple[str, str]:
    """从环境变量读取 Alpaca API 密钥。

    密钥不允许写入配置文件或代码，只能通过环境变量注入：
        ALPACA_API_KEY / ALPACA_SECRET_KEY
    """
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        raise RuntimeError(
            "未找到 Alpaca API 密钥，请先设置环境变量 "
            "ALPACA_API_KEY 与 ALPACA_SECRET_KEY"
        )
    return key, secret

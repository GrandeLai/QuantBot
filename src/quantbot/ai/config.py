"""主交易链路配置；密钥只通过环境变量注入。"""

from __future__ import annotations

from copy import deepcopy
from importlib.resources import files
import math
import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit

from quantbot.evidence import hash_payload


def _number(value, name, lower, upper, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} 必须为有限数值")
    if not lower <= value <= upper or (positive and value == lower):
        raise ValueError(f"{name} 超出允许范围")


def validate_endpoint(url: str) -> str:
    parsed = urlsplit(url)
    local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (parsed.scheme != "https" and not (parsed.scheme == "http" and local)) or not parsed.hostname:
        raise ValueError("模型接口必须使用 HTTPS，本机模型可使用 HTTP")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("模型接口地址不能携带凭据、查询参数或片段")
    return url.rstrip("/")


def load_config(path: str | Path | None = None, environ=None) -> dict:
    defaults = tomllib.loads(files("quantbot").joinpath("resources/default.toml").read_text())
    config = deepcopy(defaults)
    if path is not None:
        supplied = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        for section, values in supplied.items():
            if section not in defaults or not isinstance(values, dict):
                raise ValueError(f"未知配置段：{section}")
            unknown = set(values) - set(defaults[section])
            if unknown:
                raise ValueError(f"未知配置项：{section}.{sorted(unknown)[0]}")
            config[section].update(values)
    env = os.environ if environ is None else environ
    for variable, key in (
        ("QUANTBOT_LLM_BACKEND", "backend"),
        ("QUANTBOT_LLM_BASE_URL", "base_url"),
        ("QUANTBOT_LLM_MODEL", "model"),
    ):
        if env.get(variable):
            config["llm"][key] = env[variable]
    validate_config(config)
    return config


def validate_config(config: dict) -> None:
    market, strategy, llm, risk, execution = (
        config[k] for k in ("market", "strategy", "llm", "risk", "execution")
    )
    symbols = market["symbols"]
    if not isinstance(symbols, list) or not symbols or len(set(symbols)) != len(symbols):
        raise ValueError("market.symbols 必须为不重复的非空股票代码列表")
    if any(not isinstance(s, str) or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", s) for s in symbols):
        raise ValueError("股票代码必须为大写代码")
    if market["benchmark"] not in symbols or market["timeframe"] != "1d":
        raise ValueError("基准必须包含在股票池内，当前主链路支持美股日线")
    if strategy["mode"] not in {"ai", "price_action"}:
        raise ValueError("strategy.mode 必须为 ai 或 price_action")
    if strategy.get("crypto_rule", "momentum_hold") not in {
        "momentum_hold", "trend_fast", "breakout", "pullback", "trend_filtered", "strength_rotation",
    }:
        raise ValueError("未知 strategy.crypto_rule")
    for key in ("crypto_cooldown_bars", "stock_cooldown_bars"):
        cooldown = strategy.get(key, 0)
        if type(cooldown) is not int or not 0 <= cooldown <= 100:
            raise ValueError(f"strategy.{key} 必须为 0..100 的整数")
    _number(strategy.get("crypto_rotation_margin", 0.01), "strategy.crypto_rotation_margin", 0, 1)
    for key in ("breakout_window", "exit_window", "atr_window", "swing_k", "momentum_window"):
        if type(strategy[key]) is not int or not 1 <= strategy[key] <= 2520:
            raise ValueError(f"strategy.{key} 必须为 1..2520 的整数")
    for key in ("atr_stop_mult", "pullback_depth_atr"):
        _number(strategy[key], f"strategy.{key}", 0, 100, positive=True)
    if strategy["momentum_filter"] not in {"absolute", "relative"}:
        raise ValueError("未知的价格行为动量模式")
    validate_endpoint(llm["base_url"])
    if not isinstance(llm["model"], str) or not isinstance(llm["api_key_env"], str) or not llm["api_key_env"]:
        raise ValueError("llm.model 和 api_key_env 必须为字符串")
    fallback = llm.get("fallback_models", [])
    if not isinstance(fallback, list) or len(fallback) > 4 or any(
        not isinstance(m, str) or not m.strip() for m in fallback
    ) or len(set(fallback)) != len(fallback) or llm["model"] in fallback:
        raise ValueError("llm.fallback_models 最多包含四个不重复的备用模型")
    if llm["response_format"] not in {"json_object", "json_schema", "none"}:
        raise ValueError("未知的模型 response_format")
    backend = llm.get("backend", "openai_compatible")
    if backend not in {"openai_compatible", "codex_cli"}:
        raise ValueError("llm.backend 必须为 openai_compatible 或 codex_cli")
    if backend == "codex_cli" and fallback:
        raise ValueError("Codex 决策不自动重试或切换模型，请清空 fallback_models")
    codex = config.get("codex", {})
    executable = codex.get("executable", "codex")
    if not isinstance(executable, str) or not executable.strip() or "\0" in executable:
        raise ValueError("codex.executable 必须为单个可执行文件路径或名称")
    if codex.get("reasoning_effort", "") not in {
        "", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
    }:
        raise ValueError("未知的 codex.reasoning_effort")
    _number(llm["timeout_seconds"], "llm.timeout_seconds", 0, 300, positive=True)
    for key in ("max_weight", "max_gross_exposure", "risk_per_trade", "max_portfolio_heat"):
        _number(risk[key], f"risk.{key}", 0, 1, positive=True)
    for key in ("cash_buffer", "min_confidence"):
        _number(risk[key], f"risk.{key}", 0, 1)
    for market_name, label in (("crypto", "加密"), ("stock", "美股")):
        stop_key, profit_key = f"{market_name}_stop_loss_pct", f"{market_name}_take_profit_pct"
        for key in (stop_key, profit_key):
            _number(risk.get(key, 0), f"risk.{key}", 0, 0.5)
        if bool(risk.get(stop_key, 0)) != bool(risk.get(profit_key, 0)):
            raise ValueError(f"{label}固定止盈止损必须同时启用或同时关闭")
    if type(risk["max_positions"]) is not int or not 1 <= risk["max_positions"] <= 100:
        raise ValueError("risk.max_positions 必须为 1..100 的整数")
    if risk["max_gross_exposure"] > 1 - risk["cash_buffer"] + 1e-12:
        raise ValueError("总暴露上限必须保留 cash_buffer")
    _number(execution["initial_cash"], "execution.initial_cash", 0, 1e12, positive=True)
    _number(execution["cost_bps"], "execution.cost_bps", 0, 1000)
    _number(execution["limit_buffer_bps"], "execution.limit_buffer_bps", execution["cost_bps"], 1000)
    _number(execution.get("max_volume_participation", 0.01), "execution.max_volume_participation", 0, 1, positive=True)
    _number(execution.get("impact_bps_at_participation_cap", 0), "execution.impact_bps_at_participation_cap", 0, 1000)
    for key, default, upper in (("latency_seconds", 0, 86400), ("entry_ttl_bars", 3, 100)):
        value = execution.get(key, default)
        if type(value) is not int or not (1 if key == "entry_ttl_bars" else 0) <= value <= upper:
            raise ValueError(f"execution.{key} 必须为范围内的整数")
    if not isinstance(execution["runtime_dir"], str) or not execution["runtime_dir"]:
        raise ValueError("execution.runtime_dir 必须为非空路径")


def config_hash(config: dict) -> str:
    return hash_payload(config)

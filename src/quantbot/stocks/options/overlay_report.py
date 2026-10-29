"""同一持仓与数据下比较持股、备兑、保护看跌和领口，保存独立研究证据。"""

from dataclasses import asdict
from datetime import UTC, datetime
import json
from pathlib import Path
from uuid import uuid4

from quantbot.evidence import hash_payload, source_tree_hash

from .overlay import OverlayAccount, OverlayPolicy, STRATEGIES, replay_overlay
from .overlay_data import OverlayData


BOUNDARY = (
    "本地规则型期权覆盖回测，不调用模型或券商；可由本地 Agent 冻结参数后调用。"
    "报价与发布时间来自输入声明，未独立认证。成交假设为下一交易日开盘窗口的历史买卖报价净额包，"
    "不代表多腿一定可同时成交。提前指派是独立压力情景，不能预测实际指派；不模拟盘后价格和相反行权指令。"
    "Greeks 使用欧式 Black-Scholes 近似，不能当作美式公允价值；资金利息、税费及冲击未建模。"
)


def options_backtest(*, payload, portfolio, policy=None, output):
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True)
    if len(encoded.encode()) > 32_000_000:
        raise ValueError("历史期权输入超过 32 MB")
    try:
        policy = OverlayPolicy(**({} if policy is None else policy))
    except TypeError as exc:
        raise ValueError(f"期权参数字段无效：{exc}") from None
    try:
        data = OverlayData(payload)
        initial = OverlayAccount(data, portfolio)
    except (TypeError, KeyError) as exc:
        raise ValueError(f"期权输入字段或类型无效：{exc}") from None
    root = Path(output).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False)
    identifier, code_hash = uuid4().hex, source_tree_hash()
    plan = {"schema_version": 1, "run_id": identifier, "registered_at": datetime.now(UTC).isoformat(),
            "data_hash": hash_payload(payload), "code_hash": code_hash,
            "portfolio": portfolio, "initial_equity": float(initial.initial_equity), "policy": asdict(policy),
            "initial_basis": "first_session_close_market_value", "initial_option_positions": "none",
            "strategies": list(STRATEGIES), "assignment_scenarios": ["expiration_only", "ex_dividend_stress"]}
    frozen = {"plan": plan, "plan_hash": hash_payload(plan), "input_data": payload}
    (root / "input.json").write_text(json.dumps(frozen, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
    results = [replay_overlay(data, portfolio, strategy, policy) for strategy in STRATEGIES]
    results += [replay_overlay(data, portfolio, strategy, policy, "ex_dividend_stress") for strategy in ("covered_call", "collar")]
    baseline = results[0]
    for result in results:
        result["excess_return_vs_hold"] = (result["return_fraction"] - baseline["return_fraction"]
                                           if result["execution_completed"] and baseline["execution_completed"] else None)
    unchanged = code_hash == source_tree_hash()
    complete = unchanged and all(r["execution_completed"] for r in results)
    report = {**frozen, "kind": "options_overlay", "execution_completed": complete,
              "research_conclusion": "insufficient" if payload["data_kind"] == "synthetic" else "not_assessed",
              "results": results, "model_calls": 0, "broker_calls": 0, "source_unchanged": unchanged,
              "data_quality": {"data_kind": payload["data_kind"], "source": payload["source"],
                  "sessions": len(data.days), "contracts": len(data.contracts), "quote_versions": len(payload["quotes"]),
                  "availability_enforced": True, "point_in_time_verified": False,
                  "corporate_action_coverage": "input_declaration_only"},
              "evidence_boundary": BOUNDARY, "report_path": str(root / "report.json")}
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
    return report

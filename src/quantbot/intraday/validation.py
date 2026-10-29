"""One-shot independent intraday validation, distinct from broker admission.

Preparation reads development evidence and metadata only. Price access starts
after an exclusive claim; a failed run consumes the claim just like a success.
"""

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import gzip
import json
import math
from pathlib import Path

from quantbot.ai.research_catalog import check_window, load, write_revision
from quantbot.evidence import hash_payload, source_tree_hash
from quantbot.factors.conditional_study import check_ref, file_ref, read, save
from quantbot.factors.time_series import holm_adjust
from . import research
from .data import checked_protocol, frozen_policy_snapshots, load_bars, session_grid


VERSION = "intraday_independent_v1"


def _study_root(path):
    path = Path(path).resolve()
    return path.parent if path.is_file() else path


def _registry_trials(catalog, registry, active=(), *, conservative_new=False):
    """Include every prior reservation; unresolved trials conservatively count as p=1."""
    known = {t["id"] for t in catalog["trials"]}
    for path in sorted(Path(registry).glob("*-reservation.json")):
        reservation = read(path)
        trial = reservation["trial"]
        if trial["id"] not in known:
            catalog["trials"].append(trial)
            known.add(trial["id"])
        if trial["id"] not in catalog["outcomes"] and trial["id"] not in active:
            outcome = path.with_name(path.name.replace("-reservation.json", "-outcome.json"))
            if outcome.exists() and not conservative_new:
                value = read(outcome)
                check_ref(value["evidence"])
            else:
                value = {"p_value": 1.0, "qualified": False, "status": "reserved_or_unresolved"}
            catalog["outcomes"][trial["id"]] = value


def _source(protocol_path, study_path):
    study = _study_root(study_path)
    started = read(study / "started.json")
    frozen = read(check_ref(started["freeze"]))
    snapshots = frozen_policy_snapshots(frozen)
    protocol = checked_protocol(protocol_path, policy_snapshots=snapshots)
    if frozen["protocol"] != file_ref(protocol_path):
        raise ValueError("开发结果与指定方案不一致")
    report_path, catalog_path = study / "report.json", study / "catalog-completed.json"
    report, catalog = read(report_path), load(catalog_path)
    if catalog["parent"] != frozen["catalog"]:
        raise ValueError("开发结果缺少冻结账本祖先")
    cards = protocol["factors"]
    if ({r["id"] for r in report["results"]} != {c["id"] for c in cards}
            or len(report["results"]) != len(cards)
            or report["family_size"] != len(catalog["trials"])
            or len(catalog["outcomes"]) != len(catalog["trials"])):
        raise ValueError("开发结果或累计试验不完整")
    for card in cards:
        if catalog["outcomes"][card["id"]]["evidence"] != file_ref(report_path):
            raise ValueError("开发结果未绑定累计研究账本")
    return protocol, frozen, catalog, report


def _audit_curve(result, protocol, market, expected_days, sessions):
    """Bind reported daily cash/equity and entry counts back to the fill ledger."""
    research.audit_report(result)
    initial = Decimal(str(protocol["markets"][market]["execution"]["initial_cash"]))
    if Decimal(str(result["initial_equity"])) != initial:
        raise ValueError("回放初始资金与冻结账户不一致")
    events = sorted(result["events"], key=lambda e: datetime.fromisoformat(e["at"]))
    fills = [e for e in events if e["status"] == "filled"]
    cash, positions, pending, index = initial, {}, set(), 0
    if (result["evaluated_start"], result["evaluated_end"]) != (expected_days[0], expected_days[-1]):
        raise ValueError("回放起止标签与冻结日期不一致")
    for day, row in zip(expected_days, result["daily_curve"]):
        opening, closing = (at.to_pydatetime() for at in sessions[day])
        if datetime.fromisoformat(row["as_of"]) != closing:
            raise ValueError("日终账目时间不属于冻结交易会话")
        while index < len(events) and datetime.fromisoformat(events[index]["at"]) <= closing:
            event = events[index]
            if datetime.fromisoformat(event["at"]) < opening:
                raise ValueError("订单事件不属于冻结评估交易会话")
            identifier, status = event["client_order_id"], event["status"]
            if status == "accepted":
                pending.add(identifier)
            elif status == "canceled":
                pending.discard(identifier)
            elif status == "filled":
                quantity = Decimal(event["quantity"]) * (1 if event["side"] == "buy" else -1)
                cash -= quantity * Decimal(event["price"])
                symbol = event["symbol"]
                positions[symbol] = positions.get(symbol, Decimal(0)) + quantity
                if not positions[symbol]:
                    positions.pop(symbol)
                # Protective orders can originate inside the matcher without a
                # separate accepted event; their remaining quantity still binds
                # whether a later fill or cancellation is required.
                if Decimal(event["remaining"]) > 0:
                    pending.add(identifier)
                else:
                    pending.discard(identifier)
            else:
                raise ValueError("回放包含未知订单状态")
            index += 1
        if (abs(cash - Decimal(str(row["cash"]))) > Decimal("0.00000001")
                or positions != {s: Decimal(q) for s, q in row["positions"].items()}
                or (not positions and abs(cash - Decimal(str(row["equity"]))) > Decimal("0.00000001"))):
            raise ValueError("日终权益曲线与逐笔成交不一致")
        if positions or pending or row["pending_orders"]:
            raise ValueError("独立日内验证不能包含隔夜持仓或未结束订单")
    if (index != len(events) or result["account"]["pending_orders"] or not math.isclose(result["net_return"],
            float(result["daily_curve"][-1]["equity"]) / float(initial) - 1, abs_tol=1e-12)
            or result["entry_orders_filled"] != len({e["client_order_id"] for e in fills if e["side"] == "buy"})
            or result["partial_fills"] != sum(e["partial"] for e in fills)
            or result["buy_fills"] != sum(e["side"] == "buy" for e in fills)):
        raise ValueError("回放净收益、成交数量或日期与账本不一致")


def _screens(protocol, card, artifacts):
    """Recompute statistics and ledger audits from hash-bound replay artifacts."""
    refs = {Path(r["path"]).name: r for r in artifacts}
    if len(refs) != len(artifacts):
        raise ValueError("回放工件名称重复")
    sessions = session_grid(protocol, card["market"])
    expected_days = {w["id"]: research.evaluation_days(protocol, sessions, w) for w in protocol["windows"]}
    scenarios = []
    for cost in protocol["execution_model"]["one_way_cost_bps"]:
        for latency in protocol["execution_model"]["latency_seconds"]:
            pairs, originals = [], []
            for window in protocol["windows"]:
                name = f"{card['id']}-{cost}-{latency}-{window['id']}.json.gz"
                if name not in refs:
                    raise ValueError("缺少完整冻结回放: " + name)
                with gzip.open(check_ref(refs[name]), "rt", encoding="utf-8") as handle:
                    branches = json.load(handle)
                expected = {"candidate": card["factor"], "control": card.get("control", "control")}
                if protocol.get("original_control_required"):
                    expected["original_control"] = "control"
                if set(branches) != set(expected):
                    raise ValueError("候选、配对对照或原始对照不完整")
                for branch, factor in expected.items():
                    result = branches[branch]
                    if any(result[k] != v for k, v in {
                        "factor": factor, "market": card["market"], "window": window["id"],
                        "cost_bps": cost, "latency_seconds": latency,
                    }.items()):
                        raise ValueError("回放身份与冻结场景不一致")
                    if branch == "candidate" and result.get("recipe") != card.get("recipe"):
                        raise ValueError("回放策略定义改变")
                    if [r["day"] for r in result["daily_curve"]] != expected_days[window["id"]]:
                        raise ValueError("回放日期与冻结窗口不一致")
                    _audit_curve(result, protocol, card["market"], expected_days[window["id"]], sessions)
                pairs.append((branches["candidate"], branches["control"]))
                if "original_control" in branches:
                    originals.append((branches["candidate"], branches["original_control"]))
            scenario = research.screen_scenario(card, pairs, protocol["statistics"], cost=cost, latency=latency)
            if originals:
                original = research.screen_scenario(
                    card, originals, protocol["statistics"], cost=cost, latency=latency)
                scenario["original_control"] = original
                scenario["p_value"] = max(scenario["p_value"], original["p_value"])
                scenario["reasons"] += ["original_control:" + r for r in original["reasons"]]
            scenarios.append(scenario)
    return scenarios


def _numeric(scenarios):
    reasons = sorted({r for scenario in scenarios for r in scenario["reasons"]})
    if not scenarios:
        reasons.append("missing_complete_scenarios")
    raw = max((s["p_value"] for s in scenarios), default=1.0)
    if not math.isfinite(raw) or not 0 <= raw <= 1:
        raise ValueError("无效统计概率")
    return {"raw_p_value": raw, "p_value": 1.0 if reasons else raw, "reasons": reasons}


def _development(card, protocol, catalog, report):
    row = next(r for r in report["results"] if r["id"] == card["id"])
    if not row["scenarios"]:
        return {"numeric_passed": False, "reasons": ["development_missing_replays"]}
    numeric = _numeric(_screens(protocol, card, report["artifacts"]))
    current = {r["id"]: _numeric(r["scenarios"])["p_value"] for r in report["results"]}
    current[card["id"]] = numeric["p_value"]
    ids = [t["id"] for t in catalog["trials"]]
    values = [current.get(t, catalog["outcomes"][t]["p_value"]) for t in ids]
    adjusted = holm_adjust(values)[ids.index(card["id"])]
    reasons = list(numeric["reasons"])
    if adjusted > protocol["statistics"]["familywise_alpha"]:
        reasons.append("development_cumulative_holm_not_significant")
    return {**numeric, "holm_p_value": adjusted, "numeric_passed": not reasons, "reasons": reasons}


def _window_check(section, protocol, catalog, market):
    span, windows = section["range"], section["windows"]
    start, end = date.fromisoformat(span["start"]), date.fromisoformat(span["end_exclusive"])
    if start >= end or not windows or len({w["id"] for w in windows}) != len(windows):
        raise ValueError("验证读取区间或窗口无效")
    prior = None
    for window in windows:
        left, right = date.fromisoformat(window["start"]), date.fromisoformat(window["end"])
        if not start <= left <= right < end or (prior is not None and left <= prior):
            raise ValueError("验证窗口越界、重叠或顺序错误")
        prior = right
    if len(windows) < protocol["statistics"]["minimum_positive_windows"]:
        raise ValueError("独立验证窗口不能少于原稳定性门槛")
    return check_window(catalog, market, protocol["markets"][market]["symbols"],
                        start.isoformat(), (end - timedelta(days=1)).isoformat(), role="independent")


def prepare(protocol_path, study_path, request_path, output):
    """Persist a blocked preparation or a frozen plan, without opening new prices."""
    study_path = _study_root(study_path)
    protocol, development_freeze, catalog, report = _source(protocol_path, study_path)
    request = read(request_path)
    if request["version"] != 1 or request["mode"] not in {"research", "synthetic"}:
        raise ValueError("未知独立验证请求版本或模式")
    catalog_parent = study_path / "catalog-completed.json"
    if request.get("catalog"):
        supplied_path = check_ref(request["catalog"])
        supplied = load(supplied_path)
        ancestor, required = request["catalog"], file_ref(catalog_parent)
        while ancestor != required:
            ancestor = read(check_ref(ancestor))["parent"]
            if ancestor is None:
                raise ValueError("后续研究账本不是开发完成账本的连续修订")
        catalog, catalog_parent = supplied, supplied_path
    selected = request["selections"]
    if len({s["market"] for s in selected}) != len(selected) or any(
            s["market"] not in {"crypto", "stocks"} for s in selected):
        raise ValueError("每市场只允许一项明确选定候选")
    checks, accepted = [], []
    registry = study_path / "independent-validations"
    _registry_trials(catalog, registry)
    reviews = {}
    eligible = {market: 0 for market in ("crypto", "stocks")}
    for card in protocol["factors"]:
        row = next(r for r in report["results"] if r["id"] == card["id"])
        if row["scenarios"] and not _numeric(row["scenarios"])["reasons"]:
            reviews[card["id"]] = _development(card, protocol, catalog, report)
            eligible[card["market"]] += int(reviews[card["id"]]["numeric_passed"])
    for path in registry.glob("*-access.json"):
        catalog["accesses"].extend(read(path)["accesses"])
    for market in ("crypto", "stocks"):
        selection = next((s for s in selected if s["market"] == market), None)
        if selection is None:
            checks.append({"market": market, "reasons": ["no_selected_development_candidate"]})
            continue
        card = next((c for c in protocol["factors"] if c["id"] == selection["candidate_id"]
                     and c["market"] == market), None)
        if card is None:
            raise ValueError("选定候选不属于开发方案")
        numeric = reviews.get(card["id"]) or _development(card, protocol, catalog, report)
        reasons = [] if numeric["numeric_passed"] else ["development_numeric_gate_failed"]
        if development_freeze["source_hash"] != source_tree_hash():
            reasons.append("development_code_requires_revalidation")
        if reasons and request["mode"] == "research":
            checks.append({"market": market, "candidate_id": card["id"], "development": numeric,
                           "reasons": reasons, "blocking_reasons": reasons})
            continue
        access = _window_check(selection, protocol, catalog, market)
        reasons.extend(access["reasons"])
        if not access["allowed_by_index"]:
            checks.append({"market": market, "candidate_id": card["id"], "development": numeric,
                           "access": access, "reasons": reasons, "blocking_reasons": reasons})
            continue
        evidence = None
        if selection.get("evidence"):
            evidence = _evidence(selection, protocol)
            if not evidence["metadata_ready"]:
                reasons.extend(r for r in evidence["reasons"] if r.startswith("missing_file:"))
        else:
            reasons.append("missing_independent_data_evidence")
        # Synthetic fixtures exercise execution but cannot certify research qualification.
        blocking = list(reasons)
        if request["mode"] == "synthetic" and evidence and evidence["synthetic"]:
            blocking = [r for r in blocking if r not in {
                "development_numeric_gate_failed", "development_code_requires_revalidation"}]
        elif evidence and evidence["synthetic"]:
            blocking.append("synthetic_not_allowed_for_research")
        checks.append({"market": market, "candidate_id": card["id"], "development": numeric,
                       "access": access, "evidence": evidence, "reasons": sorted(set(reasons)),
                       "blocking_reasons": sorted(set(blocking))})
        if not blocking:
            accepted.append({"market": market, "card": card, "range": selection["range"],
                             "windows": selection["windows"], "evidence": evidence,
                             "evidence_input": file_ref(selection["evidence"])})
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    preparation = {"version": VERSION, "mode": request["mode"], "checks": checks,
                   "eligible_candidate_count": eligible,
                   "status": "ready" if accepted else "blocked", "price_data_read": False,
                   "formal_admission": False, "trading_enabled": False,
                   "broker_paper_authorized": False, "live_authorized": False}
    save(output / "preparation.json", preparation)
    if not accepted:
        return preparation
    # Reserve every intended validation before any price access, including failures.
    frozen_at = datetime.now(UTC).isoformat()
    statistics = deepcopy(protocol["statistics"])
    for item in accepted:
        trial = "independent:" + hash_payload({"candidate": item["card"], "range": item["range"],
                                               "windows": item["windows"]})
        if any(t["id"] == trial for t in catalog["trials"]):
            raise ValueError("该独立验证已经登记，不能换目录重复试验")
        item["trial_id"] = trial
        catalog["trials"].append({"id": trial, "market": item["market"], "definition": item,
                                   "registered_at": frozen_at})
    statistics["minimum_family_size"] = len(catalog["trials"])
    write_revision(catalog, output / "catalog-frozen.json", parent=catalog_parent)
    plan = {"version": VERSION, "mode": request["mode"], "protocol": file_ref(protocol_path),
            "development_study": str(Path(study_path).resolve()), "request": file_ref(request_path),
            "development_freeze": development_freeze, "development_report": file_ref(Path(study_path) / "report.json"),
            "catalog": file_ref(output / "catalog-frozen.json"), "source_hash": source_tree_hash(),
            "development_source_hash": development_freeze["source_hash"],
            "development_code_matches": development_freeze["source_hash"] == source_tree_hash(),
            "registry": str(registry),
            "frozen_at": frozen_at, "selected": accepted, "statistics": statistics,
            "strategy_contract": {k: protocol[k] for k in ("sampling", "markets", "execution_model")},
            "model_calls_authorized": 0, "formal_admission": False, "trading_enabled": False}
    save(output / "validation-freeze.json", plan)
    save(output / "validation-freeze.binding.json", {"freeze": file_ref(output / "validation-freeze.json")})
    registry.mkdir(exist_ok=True)
    for item in accepted:
        save(registry / (item["trial_id"].split(":")[1] + "-reservation.json"), {
            "trial_id": item["trial_id"], "freeze": file_ref(output / "validation-freeze.json"),
            "trial": next(t for t in catalog["trials"] if t["id"] == item["trial_id"])})
    return preparation | {"freeze": file_ref(output / "validation-freeze.json")}


def _evidence(selection, protocol):
    # Kept at this boundary to avoid importing optional acquisition dependencies.
    from .evidence import inspect_evidence

    return inspect_evidence(selection["evidence"], market=selection["market"],
        symbols=protocol["markets"][selection["market"]]["symbols"],
        read_start=selection["range"]["start"],
        read_end=(date.fromisoformat(selection["range"]["end_exclusive"]) - timedelta(days=1)).isoformat(),
        execution_model=protocol["execution_model"])


def _checked_plan(path):
    path = Path(path).resolve()
    if read(path.with_name("validation-freeze.binding.json"))["freeze"] != file_ref(path):
        raise ValueError("独立验证冻结计划已改变")
    plan = read(path)
    if plan["version"] != VERSION or plan["source_hash"] != source_tree_hash():
        raise ValueError("独立验证代码或版本改变")
    check_ref(plan["request"])
    check_ref(plan["development_report"])
    snapshots = frozen_policy_snapshots(plan["development_freeze"])
    protocol = checked_protocol(check_ref(plan["protocol"]), policy_snapshots=snapshots)
    if plan["strategy_contract"] != {k: protocol[k] for k in ("sampling", "markets", "execution_model")}:
        raise ValueError("冻结策略、止盈止损或撮合契约改变")
    catalog = load(check_ref(plan["catalog"]))
    if Path(plan["registry"]) != Path(plan["development_study"]) / "independent-validations":
        raise ValueError("独立验证登记位置改变")
    expected = deepcopy(protocol["statistics"])
    expected["minimum_family_size"] = len(catalog["trials"])
    if plan["statistics"] != expected:
        raise ValueError("独立验证统计门槛改变")
    for item in plan["selected"]:
        reservation = read(Path(plan["registry"]) / (item["trial_id"].split(":")[1] + "-reservation.json"))
        if (reservation["trial_id"] != item["trial_id"] or reservation["freeze"] != file_ref(path)
                or reservation["trial"] != next(t for t in catalog["trials"] if t["id"] == item["trial_id"])):
            raise ValueError("独立验证登记身份不一致，不能复制冻结计划重跑")
    return plan, protocol, catalog


def run(freeze_path, output):
    """Consume a frozen plan once; keep its claim and failure evidence on errors."""
    plan, protocol, catalog = _checked_plan(freeze_path)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    # A shared registry is anchored to the canonical development study: changing
    # output, re-preparing, or copying the freeze cannot create a new read budget.
    claim = Path(freeze_path).resolve().with_name("validation-access.json")
    registry = Path(plan["registry"])
    lock = registry / "access.lock"
    save(lock, {"freeze": file_ref(freeze_path), "started_at": datetime.now(UTC).isoformat()})
    try:
        for path in registry.glob("*-access.json"):
            catalog["accesses"].extend(read(path)["accesses"])
        accesses = []
        for item in plan["selected"]:
            access = _window_check(item, protocol, catalog, item["market"])
            if not access["allowed_by_index"]:
                raise ValueError("独立窗口已经暴露: " + ",".join(access["reasons"]))
            accesses.append({"id": item["trial_id"], "market": item["market"],
                "symbols": protocol["markets"][item["market"]]["symbols"], "start": item["range"]["start"],
                "end": (date.fromisoformat(item["range"]["end_exclusive"]) - timedelta(days=1)).isoformat(),
                "purpose": "single_access_independent_validation", "role": "independent"})
        save(claim, {"freeze": file_ref(freeze_path), "output": str(output),
                     "started_at": datetime.now(UTC).isoformat(), "accesses": accesses})
        for access in accesses:
            save(registry / (access["id"].split(":")[1] + "-access.json"), {
                "freeze": file_ref(freeze_path), "accesses": [access]})
        catalog["accesses"].extend(accesses)
    finally:
        lock.unlink()
    write_revision(catalog, output / "catalog-accessed.json", parent=check_ref(plan["catalog"]))
    artifacts, results = [], []
    try:
        for item in plan["selected"]:
            check_ref(item["evidence_input"])
            selection = item | {"evidence": item["evidence_input"]["path"]}
            evidence = _evidence(selection, protocol)
            if evidence != item["evidence"]:
                raise ValueError("冻结证据元数据改变")
            own = protocol | {"range": item["range"], "windows": item["windows"], "statistics": plan["statistics"]}
            from .evidence import verify_market_evidence

            verification = verify_market_evidence(own, item["market"], {
                "data": evidence["data_ref"], "evidence": item["evidence_input"]})
            if not verification.get("consistency_verified") or not verification.get("execution_compatible"):
                raise ValueError("独立验证证据未通过: " + ",".join(verification["reasons"]))
            frame, sessions, quality = load_bars(check_ref(evidence["data_ref"]), own, item["market"])
            if not quality["complete"]:
                raise ValueError("独立分钟数据不完整")
            prepared = research.prepare_sessions(frame, sessions, own["markets"][item["market"]]["symbols"],
                atr_window=own["markets"][item["market"]]["strategy"]["atr_window"],
                actions=verification["checks"]["corporate_actions"]["actions"])
            del frame
            card = item["card"]
            for cost in own["execution_model"]["one_way_cost_bps"]:
                for latency in own["execution_model"]["latency_seconds"]:
                    for window in own["windows"]:
                        factors = {"candidate": card["factor"], "control": card.get("control", "control")}
                        if own.get("original_control_required"):
                            factors["original_control"] = "control"
                        branches = {branch: research.replay(own, item["market"], prepared, window, factor, cost, latency,
                                    recipe=card.get("recipe") if branch == "candidate" else None)
                                    for branch, factor in factors.items()}
                        for result in branches.values():
                            research.audit_report(result)
                        path = output / f"{card['id']}-{cost}-{latency}-{window['id']}.json.gz"
                        with gzip.open(path, "xt", encoding="utf-8") as handle:
                            json.dump(branches, handle, ensure_ascii=False, allow_nan=False)
                        artifacts.append(file_ref(path))
            scenarios = _screens(own, card, artifacts)
            results.append({"id": item["trial_id"], "candidate_id": card["id"], "market": item["market"],
                            "scenarios": scenarios, "data_evidence": verification, **_numeric(scenarios)})
        _checked_plan(freeze_path)
        for result in results:
            for reference in result["data_evidence"]["checked_references"]:
                check_ref(reference)
        report = _qualification(plan, catalog, results)
        report.update(freeze=file_ref(freeze_path), access=file_ref(claim), artifacts=artifacts,
                      model_calls=0, broker_calls=0)
        save(output / "validation-report.json", report)
        for result in results:
            catalog["outcomes"][result["id"]] = {"p_value": result["p_value"],
                "qualified": result["strategy_qualified"], "status": "independent_validation_completed",
                "evidence": file_ref(output / "validation-report.json")}
        write_revision(catalog, output / "catalog-completed.json", parent=output / "catalog-accessed.json")
        _record_outcomes(plan, catalog)
        return report
    except Exception as exc:
        save(output / "failure.json", {"type": type(exc).__name__, "error": str(exc),
            "freeze": file_ref(freeze_path), "artifacts": artifacts, "partial_results": results,
            "access_consumed": True, "trading_enabled": False, "formal_admission": False})
        for item in plan["selected"]:
            catalog["outcomes"][item["trial_id"]] = {"p_value": 1.0, "qualified": False,
                "status": "independent_validation_failed", "evidence": file_ref(output / "failure.json")}
        write_revision(catalog, output / "catalog-completed.json", parent=output / "catalog-accessed.json")
        _record_outcomes(plan, catalog)
        raise


def _record_outcomes(plan, catalog):
    for item in plan["selected"]:
        save(Path(plan["registry"]) / (item["trial_id"].split(":")[1] + "-outcome.json"),
             catalog["outcomes"][item["trial_id"]])


def _qualification(plan, catalog, results):
    _registry_trials(catalog, plan["registry"], active=[r["id"] for r in results], conservative_new=True)
    historical = [catalog["outcomes"][t["id"]]["p_value"] for t in catalog["trials"]
                  if t["id"] in catalog["outcomes"]]
    adjusted = holm_adjust(historical + [r["p_value"] for r in results])[len(historical):]
    for row, p in zip(results, adjusted):
        item = next(s for s in plan["selected"] if s["trial_id"] == row["id"])
        numeric_reasons = list(row["reasons"])
        if p > plan["statistics"]["familywise_alpha"]:
            numeric_reasons.append("independent_cumulative_holm_not_significant")
        evidence = row["data_evidence"]
        verified = evidence["evidence_verified"] and not item["evidence"]["synthetic"]
        row.update(holm_p_value=p, numeric_passed=not numeric_reasons,
                   evidence_verified=verified, authenticity_verified=evidence["authenticity_verified"],
                   strategy_qualified=not numeric_reasons and verified
                   and plan["mode"] == "research", reasons=numeric_reasons + evidence["reasons"])
    return {"version": VERSION, "results": results, "family_size": len(catalog["trials"]),
            "qualified_candidates": [r["candidate_id"] for r in results if r["strategy_qualified"]],
            "formal_admission": False, "trading_enabled": False, "broker_paper_authorized": False,
            "live_authorized": False, "sealed_holdout_evaluated": False,
            "boundary": "独立研究资格不等于任何既有市场政策准出或券商执行许可。"}


def assess(freeze_path, report_path):
    """Rebuild qualification from persisted ledgers; ignore caller approval flags."""
    plan, protocol, catalog = _checked_plan(freeze_path)
    report = read(report_path)
    if report["freeze"] != file_ref(freeze_path):
        raise ValueError("资格报告与冻结计划不一致")
    access = read(check_ref(report["access"]))
    if access["freeze"] != report["freeze"] or Path(access["output"]) != Path(report_path).resolve().parent:
        raise ValueError("资格报告缺少唯一访问身份")
    if Path(report["access"]["path"]) != Path(freeze_path).resolve().with_name("validation-access.json"):
        raise ValueError("资格报告使用了另一访问记录")
    completed = load(Path(report_path).resolve().parent / "catalog-completed.json")
    accessed_path = Path(report_path).resolve().parent / "catalog-accessed.json"
    accessed = load(accessed_path)
    if completed["parent"] != file_ref(accessed_path) or accessed["parent"] != plan["catalog"]:
        raise ValueError("完成账本不是本次独立验证的连续修订")
    for item in plan["selected"]:
        access_record = read(Path(plan["registry"]) / (item["trial_id"].split(":")[1] + "-access.json"))
        outcome = read(Path(plan["registry"]) / (item["trial_id"].split(":")[1] + "-outcome.json"))
        if (access_record["freeze"] != report["freeze"]
                or completed["outcomes"][item["trial_id"]]["evidence"] != file_ref(report_path)
                or outcome != completed["outcomes"][item["trial_id"]]
                or outcome["status"] != "independent_validation_completed"):
            raise ValueError("资格报告不是已完成独立验证的绑定产物")
    results = []
    for item in plan["selected"]:
        check_ref(item["evidence_input"])
        own = protocol | {"range": item["range"], "windows": item["windows"], "statistics": plan["statistics"]}
        from .evidence import verify_market_evidence

        verification = verify_market_evidence(own, item["market"], {
            "data": item["evidence"]["data_ref"], "evidence": item["evidence_input"]})
        scenarios = _screens(own, item["card"], report["artifacts"])
        results.append({"id": item["trial_id"], "candidate_id": item["card"]["id"], "market": item["market"],
                        "scenarios": scenarios, "data_evidence": verification, **_numeric(scenarios)})
    return _qualification(plan, catalog, results)

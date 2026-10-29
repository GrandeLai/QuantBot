from copy import deepcopy
import io
import json

import pytest

from quantbot.ai.research_catalog import load, write_revision
from quantbot.evidence import hash_payload
from quantbot.factors.conditional_study import file_ref, read, save
from quantbot.intraday import data, mining, research
from quantbot.intraday.strategies import SessionSignals
from tests.test_intraday_execution import section, model  # noqa: F401
from tests.test_intraday_mechanisms import group


def test_search_is_complete_canonical_and_never_relabels_old_trials():
    cards, skipped = mining.generate_candidates(mining.DEFAULT_SPACE, {"trials": []})
    assert len(cards) == len({c["id"] for c in cards}) == 36 and not skipped
    for market in ("crypto", "stocks"):
        own = [c for c in cards if c["market"] == market]
        assert len(own) == 18
        assert sum(len(c["recipe"]["all"]) == 1 for c in own) == 9
    reordered = deepcopy(mining.DEFAULT_SPACE)
    reordered["seeds"].reverse()
    reordered["atoms"].reverse()
    assert mining.generate_candidates(reordered, {"trials": []})[0] == cards
    # JSON integers and equivalent float thresholds must not evade deduplication.
    reordered["atoms"][1]["threshold"] = 2
    assert mining.generate_candidates(reordered, {"trials": []})[0] == cards
    remaining, skipped = mining.generate_candidates(reordered, {"trials": cards[:3]})
    assert len(remaining) == 33 and set(skipped) == {c["id"] for c in cards[:3]}
    with pytest.raises(ValueError, match="全部登记"):
        mining.generate_candidates(reordered, {"trials": cards})


@pytest.mark.parametrize("change", [
    {"max_candidates": 35}, {"max_candidates": 65}, {"max_terms": 3},
    {"atoms": [{"feature": "future_return", "op": "gte", "threshold": 0}]},
    {"atoms": [{"feature": "close_location", "op": "eval", "threshold": 0}]},
    {"atoms": [{"feature": "close_location", "op": "gte", "threshold": float("nan")}]},
    {"atoms": [mining.DEFAULT_SPACE["atoms"][0]] * 2},
])
def test_unbounded_unknown_or_duplicate_search_fails_before_data(change):
    with pytest.raises(ValueError):
        mining.generate_candidates(mining.DEFAULT_SPACE | change, {"trials": []})


def test_features_have_exact_completed_bar_timing_and_no_future_mutation():
    session = SessionSignals()
    session.update(group(close=100), eligible=False)
    session.update(group(opening=100, high=103, low=99, close=102), eligible=False)
    now = session.update(group(opening=102, high=105, low=102, close=104), eligible=True)
    assert now["signed_efficiency_30m"] == 1
    assert now["net_move_30m_bps"] == pytest.approx(400)
    assert now["prior_range_bps"] == pytest.approx((103 - 99) / 104 * 10000)
    assert now["close_location"] == pytest.approx(2 / 3)
    saved = now.copy()
    session.update(group(opening=200, high=201, low=199, close=200), eligible=True)
    assert now == saved
    assert SessionSignals().update(group(), eligible=False)["signed_efficiency_30m"] is None
    recipe = {"all": [{"feature": "signed_efficiency_30m", "op": "gte", "threshold": 0.35}]}
    assert not mining.matches(recipe, {"signed_efficiency_30m": None}, 70)
    assert mining.matches(recipe, now, 70)


def test_net_move_requires_absolute_directional_room_not_only_efficiency():
    recipe = {"all": [{"feature": "net_move_cost_room", "op": "gte", "threshold": 1.0}]}
    session = SessionSignals()
    assert session.update(group(close=100), eligible=False)["net_move_30m_bps"] is None
    session.update(group(close=100.1), eligible=False)
    small = session.update(group(close=100.2), eligible=True)
    assert small["signed_efficiency_30m"] == 1
    assert small["net_move_30m_bps"] == pytest.approx(20)
    assert not mining.matches(recipe, small, 70)
    assert mining.matches(recipe, {"net_move_30m_bps": 70}, 70)
    assert not mining.matches(recipe, {"net_move_30m_bps": 70}, 71)
    assert not mining.matches(recipe, {"net_move_30m_bps": -70}, 70)
    assert not mining.matches(recipe, {"net_move_30m_bps": None}, 70)


def test_v2_search_preserves_old_identities_and_v1_contract():
    old_cards, _ = mining.generate_candidates(mining.DEFAULT_SPACE, {"trials": []})
    assert mining.generate_candidates(mining.DEFAULT_SPACE | {"version": 2}, {"trials": []})[0] == old_cards
    with pytest.raises(ValueError, match="全部登记"):
        mining.generate_candidates(mining.DEFAULT_SPACE | {"version": 2}, {"trials": old_cards})
    space = mining.DEFAULT_SPACE | {"version": 2, "seeds": ["control", "vwap_reclaim"],
        "atoms": [{"feature": "net_move_cost_room", "op": "gte", "threshold": 1.0}],
        "max_terms": 1, "max_candidates": 4}
    cards, _ = mining.generate_candidates(space, {"trials": old_cards})
    assert len(cards) == 4 and len({c["definition_hash"] for c in cards}) == 2
    with pytest.raises(ValueError, match="未知因子"):
        mining.generate_candidates(space | {"version": 1}, {"trials": []})


def test_v3_adds_only_pair_atom_and_preserves_historical_ids():
    old_cards, _ = mining.generate_candidates(mining.DEFAULT_SPACE, {"trials": []})
    assert hash_payload(old_cards) == "dd770eb97643819e87ed8fa01f41775fdd05057e4541428603f81fb23202ccc4"
    assert mining.generate_candidates(mining.DEFAULT_SPACE | {"version": 3}, {"trials": []})[0] == old_cards
    own_space = mining.DEFAULT_SPACE | {"version": 2, "seeds": ["control", "vwap_reclaim"],
        "atoms": [{"feature": "net_move_cost_room", "op": "gte", "threshold": 1.0}],
        "max_terms": 1, "max_candidates": 4}
    own_cards, _ = mining.generate_candidates(own_space, {"trials": []})
    assert [c["id"] for c in own_cards] == [
        "auto_crypto_6c27e7a86d22094e", "auto_crypto_572b50d437899807",
        "auto_stocks_6c27e7a86d22094e", "auto_stocks_572b50d437899807",
    ]
    assert mining.generate_candidates(own_space | {"version": 3}, {"trials": []})[0] == own_cards
    with pytest.raises(ValueError, match="全部登记"):
        mining.generate_candidates(own_space | {"version": 3}, {"trials": own_cards})
    pair_space = own_space | {"version": 3,
        "atoms": [{"feature": "pair_net_move_cost_room", "op": "gte", "threshold": 1.0}]}
    pair_cards, skipped = mining.generate_candidates(pair_space, {"trials": old_cards + own_cards})
    assert len(pair_cards) == 4 and len({c["definition_hash"] for c in pair_cards}) == 2 and not skipped
    assert {c["control"] for c in pair_cards} == {"control", "vwap_reclaim"}
    assert set(mining.FEATURES_V3) - set(mining.FEATURES_V2) == {"pair_net_move_cost_room"}
    for version in (1, 2):
        with pytest.raises(ValueError, match="未知因子"):
            mining.generate_candidates(pair_space | {"version": version}, {"trials": []})


def test_pair_cost_room_uses_same_fixed_denominator_and_missing_is_false():
    recipe = {"all": [{"feature": "pair_net_move_cost_room", "op": "gte", "threshold": 1.0}]}
    assert mining.matches(recipe, {"pair_net_move_30m_bps": 70}, 70)
    assert not mining.matches(recipe, {"pair_net_move_30m_bps": 70}, 71)
    for move in (None, -70, 69.99, float("nan")):
        assert not mining.matches(recipe, {"pair_net_move_30m_bps": move, "net_move_30m_bps": 140}, 70)
    assert not mining.matches(recipe, {"net_move_30m_bps": 140}, 70)


def test_v4_adds_only_next_bar_seed_and_preserves_historical_ids():
    for version, feature in ((1, "close_location"), (2, "net_move_cost_room"),
                             (3, "pair_net_move_cost_room")):
        old_space = mining.DEFAULT_SPACE | {"version": version,
            "atoms": [{"feature": feature, "op": "gte", "threshold": 1.0}]}
        cards, _ = mining.generate_candidates(old_space, {"trials": []})
        assert mining.generate_candidates(old_space | {"version": 4}, {"trials": []})[0] == cards
        with pytest.raises(ValueError, match="全部登记"):
            mining.generate_candidates(old_space | {"version": 4}, {"trials": cards})
        with pytest.raises(ValueError, match="种子"):
            mining.generate_candidates(old_space | {"seeds": ["breakout_next_bar"]}, {"trials": []})
    assert mining.FEATURES_BY_VERSION[4] == mining.FEATURES_V3
    space = mining.DEFAULT_SPACE | {"version": 4, "seeds": ["breakout_next_bar"],
        "atoms": [{"feature": "pair_net_move_cost_room", "op": "gte", "threshold": 1.0}],
        "max_terms": 1, "max_candidates": 2}
    cards, skipped = mining.generate_candidates(space, {"trials": []})
    assert len(cards) == 2 and not skipped
    assert {c["market"] for c in cards} == {"crypto", "stocks"}
    assert {c["control"] for c in cards} == {"breakout_next_bar"}
    assert all(c["recipe"]["seed"] == "breakout_next_bar" for c in cards)
    with pytest.raises(ValueError, match="种子"):
        mining.generate_candidates(space | {"seeds": ["breakout_arbitrary_wait"]}, {"trials": []})


def test_filtered_first_event_cannot_be_replaced_by_later_success():
    session = SessionSignals()
    for _ in range(4):
        session.update(group(), eligible=False)
    recipe = {"version": 1, "seed": "range_breakdown", "all": [
        {"feature": "close_location", "op": "gte", "threshold": 0.75}]}
    first = session.update(group(opening=99, high=100, low=98, close=98.5), eligible=True)
    later = session.update(group(opening=98, high=99, low=97, close=98.8), eligible=True)
    assert first["range_breakdown"] and not later["range_breakdown"]
    assert mining.matches(recipe, later, 70)
    for value in (first, later):
        assert not research.select_signal("mined", value, "SPY", None, {}, 70, recipe=recipe)


@pytest.fixture
def completed_parent(tmp_path, monkeypatch):
    """A completed parent with immutable evidence, no price data or external service."""
    root = tmp_path / "parent"
    root.mkdir()
    cards = [{"id": f"parent_{i}", "market": "crypto" if i < 2 else "stocks", "factor": "control"} for i in range(4)]
    parent = {"id": "dual_market_intraday_v3", "factors": cards,
              "sampling": {}, "execution_model": {}, "markets": {"crypto": {}, "stocks": {}}, "range": {}, "windows": [],
              "policies": [], "protected_intervals": [], "prior_protocol": {}, "data_policy": {},
              "statistics": {"minimum_family_size": 151, "familywise_alpha": 0.05,
                             "historical_trials": 147, "reserved_new_trials": 4},
              "trading_enabled": False, "formal_admission": False, "sealed_holdout_evaluated": False,
              "model_calls_authorized": 0}
    save(root / "PROTOCOL.json", parent)
    manifest = {"protocol": file_ref(root / "PROTOCOL.json"), **{
        m: {"data": None, "reasons": ["fixture_missing_data"]} for m in ("crypto", "stocks")}}
    save(root / "data-manifest.json", manifest)
    trials = [{"id": f"old_{i}", "market": "crypto", "definition": {}, "registered_at": "2026-10-24T00:00:00+00:00"}
              for i in range(147)] + [{"id": c["id"], "market": c["market"], "definition": c,
                                       "registered_at": "2026-10-24T00:00:00+00:00"} for c in cards]
    catalog = {"version": 1, "scope": "offline_development_index", "parent": None, "trials": trials,
               "outcomes": {t["id"]: {"p_value": 1.0, "qualified": False} for t in trials[:147]},
               "datasets": [], "accesses": [], "policies": [], "protected_intervals": [],
               "formal_admission": False, "trading_enabled": False}
    write_revision(catalog, root / "catalog-frozen.json")
    save(root / "execution-freeze.json", {"protocol": file_ref(root / "PROTOCOL.json"),
         "data_manifest": file_ref(root / "data-manifest.json"), "catalog": file_ref(root / "catalog-frozen.json")})
    save(root / "started.json", {"freeze": file_ref(root / "execution-freeze.json")})
    save(root / "report.json", {"family_size": 151, "results": [{"id": c["id"]} for c in cards]})
    for card in cards:
        catalog["outcomes"][card["id"]] = {"p_value": 1.0, "qualified": False, "evidence": file_ref(root / "report.json")}
    write_revision(catalog, root / "catalog-completed.json", parent=root / "catalog-frozen.json")
    # Isolate legacy protocol schema; the new grammar/ancestry validators remain real.
    monkeypatch.setattr(data, "checked_protocol", lambda path: read(path))
    monkeypatch.setattr(research, "checked_protocol", lambda path: read(path))
    return root, parent


def test_planner_does_not_read_prices_and_all_trials_survive_blocked_run(completed_parent, tmp_path, monkeypatch):
    root, _ = completed_parent
    def no_prices(*args, **kwargs):
        pytest.fail("planner/blocked run must not read prices")
    monkeypatch.setattr(research, "load_bars", no_prices)
    out = tmp_path / "mined"
    stdout = io.StringIO()
    assert research.main(["mine", "--protocol", str(root / "PROTOCOL.json"), "--study", str(root),
                          "--data-manifest", str(root / "data-manifest.json"), "--output", str(out)], stdout=stdout) == 0
    status = json.loads(stdout.getvalue())
    assert status["candidate_count"] == 36 and status["family_size"] == 187
    assert not status["execution_completed"] and not status["trading_enabled"]
    summary = read(out / "study/mining-summary.json")
    assert len(summary["all_candidates"]) == 36
    assert all(r["status"] == "blocked_data" and not r["numeric_screen_passed"] for r in summary["all_candidates"])
    drafts = read(out / "study/strategy-drafts.json")
    assert all(not s["enabled"] for s in drafts["strategies"])
    old, new = load(root / "catalog-completed.json"), load(out / "study/catalog-completed.json")
    assert len(new["outcomes"]) == len(new["trials"]) == 187
    assert all(new["outcomes"][k] == v for k, v in old["outcomes"].items())
    assert len(list((out / "study").glob("*-screen.json"))) == 36
    with pytest.raises(ValueError, match="全部登记"):
        mining.plan(out / "PROTOCOL.json", out / "study", out / "data-manifest.json", tmp_path / "again")
    assert not (tmp_path / "again").exists()


def test_mine_inherits_archived_policy_through_planning_freeze_and_run(completed_parent, tmp_path, monkeypatch):
    root, parent = completed_parent
    policy = tmp_path / "policy.py"
    policy.write_text("LIMIT = 0.05\n")
    original = file_ref(policy)
    snapshot = tmp_path / "archived.py"
    snapshot.write_bytes(policy.read_bytes())
    policy.write_text("LIMIT = 0.05  # current implementation\n")
    mapping = [{"original": original, "snapshot": file_ref(snapshot), "current": file_ref(policy)}]
    save(root / "mapping.json", mapping)
    parent["policies"] = [original]
    (root / "PROTOCOL.json").write_text(json.dumps(parent))
    manifest = read(root / "data-manifest.json") | {"protocol": file_ref(root / "PROTOCOL.json")}
    (root / "data-manifest.json").write_text(json.dumps(manifest))
    execution = read(root / "execution-freeze.json") | {
        "protocol": file_ref(root / "PROTOCOL.json"), "data_manifest": file_ref(root / "data-manifest.json"),
        "policy_snapshots": mapping, "policy_snapshots_input": file_ref(root / "mapping.json")}
    (root / "execution-freeze.json").write_text(json.dumps(execution))
    (root / "started.json").write_text(json.dumps({"freeze": file_ref(root / "execution-freeze.json")}))

    def check_protocol(path, *, policy_snapshots=()):
        value = read(path)
        data._check_policies(value["policies"], policy_snapshots)
        return value

    monkeypatch.setattr(data, "checked_protocol", check_protocol)
    monkeypatch.setattr(research, "checked_protocol", check_protocol)
    out = tmp_path / "archived-mining"
    stream = io.StringIO()
    research.main(["mine", "--protocol", str(root / "PROTOCOL.json"), "--study", str(root),
                   "--data-manifest", str(root / "data-manifest.json"), "--output", str(out)], stdout=stream)
    result = json.loads(stream.getvalue())
    assert result["policy_snapshots"] == file_ref(out / "policy-snapshots.json")
    assert read(out / "execution/execution-freeze.json")["policy_snapshots"] == mapping
    assert not result["trading_enabled"] and not result["execution_completed"]
    # The inherited mapping still verifies current policy bytes on continuation.
    policy.write_text("LIMIT = 0.99\n")
    with pytest.raises(ValueError, match="冻结输入"):
        mining.plan(out / "PROTOCOL.json", out / "study", out / "data-manifest.json", tmp_path / "tampered")
    assert not (tmp_path / "tampered").exists()


@pytest.mark.parametrize("version,feature,definitions,seeds", [
    (2, "net_move_cost_room", mining.FEATURES_V2, ["control", "vwap_reclaim"]),
    (3, "pair_net_move_cost_room", mining.FEATURES_V3, ["control", "vwap_reclaim"]),
    (4, "pair_net_move_cost_room", mining.FEATURES_V3, ["breakout_next_bar"]),
])
def test_planner_inherits_latest_exposure_chain_and_freezes_new_grammar(
    completed_parent, tmp_path, version, feature, definitions, seeds,
):
    root, parent = completed_parent
    catalog = load(root / "catalog-completed.json")
    exposure = {"id": "evidence-check", "market": "crypto", "symbols": ["BTC/USDT"],
                "start": "2025-01-01", "end": "2025-02-01", "role": "development"}
    catalog["accesses"].append(exposure)
    latest = tmp_path / "latest-catalog.json"
    write_revision(catalog, latest, parent=root / "catalog-completed.json")
    space = mining.DEFAULT_SPACE | {"version": version, "seeds": seeds,
        "atoms": [{"feature": feature, "op": "gte", "threshold": 1.0}],
        "max_terms": 1, "max_candidates": 4}
    out = tmp_path / "continued"
    stream = io.StringIO()
    save(tmp_path / "search.json", space)
    research.main(["mine", "--protocol", str(root / "PROTOCOL.json"), "--study", str(root),
                   "--data-manifest", str(root / "data-manifest.json"), "--catalog", str(latest),
                   "--search-space", str(tmp_path / "search.json"), "--output", str(out)], stdout=stream)
    protocol = read(out / "PROTOCOL.json")
    assert protocol["prior_catalog"] == file_ref(latest)
    assert protocol["feature_definitions"] == definitions
    assert read(out / "study/catalog-completed.json")["accesses"] == [exposure]
    assert json.loads(stream.getvalue())["family_size"] == 151 + 2 * len(seeds)
    assert len(protocol["factors"]) == 2 * len(seeds)
    assert {c["control"] for c in protocol["factors"]} == set(seeds)
    # Legacy definitions remain the original three; later grammars cannot rewrite them.
    old = tmp_path / "legacy"
    mining.plan(root / "PROTOCOL.json", root, root / "data-manifest.json", old)
    legacy = read(old / "PROTOCOL.json")
    assert legacy["feature_definitions"] == mining.FEATURES
    mining.validate_protocol(legacy, parent, load(root / "catalog-completed.json"))
    protocol["feature_definitions"]["signed_efficiency_30m"] = "changed formula"
    with pytest.raises(ValueError, match="特征定义"):
        mining.validate_protocol(protocol, parent, catalog)


def test_planner_rejects_unrelated_exposure_catalog_before_creating_plan(completed_parent, tmp_path):
    root, _ = completed_parent
    catalog = load(root / "catalog-completed.json")
    unrelated = tmp_path / "unrelated.json"
    write_revision(catalog, unrelated)
    with pytest.raises(ValueError, match="连续修订"):
        mining.plan(root / "PROTOCOL.json", root, root / "data-manifest.json", tmp_path / "invalid",
                    catalog_path=unrelated)
    assert not (tmp_path / "invalid").exists()


@pytest.mark.parametrize("field,value", [
    ("factors", []), ("independent_validation", True), ("new_outcomes_seen", True),
    ("execution_model", {"one_way_cost_bps": [0]}), ("sampling", {"max_holding_minutes": 999}),
    ("statistics", {"minimum_family_size": 1}), ("protected_intervals", [{"start": "changed"}]),
])
def test_mining_validator_rejects_removed_trials_and_weaker_limits(completed_parent, tmp_path, field, value):
    root, parent = completed_parent
    output = tmp_path / "plan"
    mining.plan(root / "PROTOCOL.json", root, root / "data-manifest.json", output)
    protocol = read(output / "PROTOCOL.json")
    protocol[field] = value
    with pytest.raises(ValueError):
        mining.validate_protocol(protocol, parent, load(root / "catalog-completed.json"))


@pytest.mark.parametrize("market", ["stocks", "crypto"])
def test_mined_recipe_uses_real_next_bar_execution_and_charges_costs(section, model, market):  # noqa: F811
    import pandas as pd
    symbols = ["SPY", "QQQ"] if market == "stocks" else ["BTC/USDT", "ETH/USDT"]
    protocol = {"range": {"start": "2025-01-02", "end_exclusive": "2025-02-15"},
                "markets": {market: section | {"symbols": symbols}}, "execution_model": model,
                "statistics": {"purge_sessions": 0, "embargo_sessions": 0}}
    sessions = data.session_grid(protocol, market)
    rows = []
    for opening, closing in sessions.values():
        for i, at in enumerate(pd.date_range(opening, closing, freq="5min", inclusive="left")):
            close = 100 if i < 6 else 100.6
            for symbol in symbols:
                rows.append([at, symbol, str(close), str(close + 0.4), str(close - 0.4), str(close), "100000"])
    prepared = research.prepare_sessions(pd.DataFrame(rows, columns=data.FIELDS), sessions, symbols)
    last = list(sessions)[-1]
    recipe = {"version": 1, "seed": "control", "all": [mining.DEFAULT_SPACE["atoms"][0]]}
    result = research.replay(protocol, market, prepared, {"id": "fixture", "start": last, "end": last},
                             "mined", 15, 1, recipe=recipe)
    assert result["recipe"] == recipe and result["entry_orders_filled"] == 2
    assert result["net_return"] < 0 and result["account"]["positions"] == {}
    assert research.audit_report(result)["status"] == "reconciled"
    for event in result["events"]:
        if event["status"] == "filled" and event["side"] == "buy":
            assert pd.Timestamp(event["at"]) > pd.Timestamp(event["created_at"])


def test_interrupted_run_keeps_reserved_trials_and_partial_evidence(completed_parent, tmp_path, monkeypatch):
    root, _ = completed_parent
    out = tmp_path / "interrupted"
    mining.plan(root / "PROTOCOL.json", root, root / "data-manifest.json", out)
    protocol_path = out / "PROTOCOL.json"
    research.freeze(protocol_path, out / "data-manifest.json", out / "execution")
    cards = read(protocol_path)["factors"]
    original_save = research.save
    def fail_second_checkpoint(path, payload):
        if path.name == cards[1]["id"] + "-screen.json":
            raise OSError("fixture disk error")
        return original_save(path, payload)
    monkeypatch.setattr(research, "save", fail_second_checkpoint)
    with pytest.raises(OSError, match="fixture disk error"):
        research.run(protocol_path, out / "execution/execution-freeze.json", out / "study")
    failure = read(out / "study/failure.json")
    assert failure["type"] == "OSError" and len(failure["partial_results"]) == 2
    assert not failure["trading_enabled"] and not failure["formal_admission"]
    assert (out / "study" / (cards[0]["id"] + "-screen.json")).exists()
    assert len(load(out / "execution/catalog-frozen.json")["trials"]) == 187
    assert not (out / "study/catalog-completed.json").exists()

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json

import pytest

from quantbot.ai.evaluation import evaluate, walk_forward
from quantbot.ai.feeds import fetch_snapshot
from quantbot.ai.research import register_study, run_study
from tests.test_ai_research import CARD, PROTOCOL
from tests.test_ai_runtime import close_time, setup


def versioned(tmp_path, market):
    store, job, _, rows = setup(tmp_path, market, count=100)
    payload = {"schema_version": 1, "market": market, "provider": "synthetic-test-fixture",
               "captured_at": "2026-01-01T00:00:00+00:00", "data_kind": "synthetic", "price_basis": "raw",
               "rows": [{**r, "available_at": close_time(market, r).isoformat()} for r in rows], "actions": []}
    revised = {**payload["rows"][80], "available_at": close_time(market, rows[85]).isoformat()}
    revised["close"] += 0.5 if market == "stocks" else 5
    payload["rows"].append(revised)
    path = tmp_path / "vintages.json"
    path.write_text(json.dumps(payload))
    job = store.create({**job["spec"], "source": "vintage", "data_path": str(path)}, job["config"])
    return store, job, path, payload


@pytest.mark.parametrize("market", ["stocks", "crypto"])
def test_future_revision_cannot_change_earlier_signals_or_past_fills(tmp_path, market):
    store, job, path, payload = versioned(tmp_path, market)
    original = fetch_snapshot(job["spec"], job["config"], datetime.now(UTC), store.root, fresh=False)
    early, later = original.times[82], original.times[86]
    before = original.context(early)
    after = original.context(later)
    # 修改未来才发布的历史修订；此前信号及撮合价格必须逐项相等。
    payload["rows"][-1]["close"] -= 0.25 if market == "stocks" else 3
    path.write_text(json.dumps(payload))
    changed = fetch_snapshot(job["spec"], job["config"], datetime.now(UTC), store.root, fresh=False)
    assert changed.context(early) == before
    assert changed.context(later).snapshot_id != after.snapshot_id
    assert changed.bars(original.times[79], original.times[81]) == original.bars(original.times[79], original.times[81])
    quality = changed.quality()
    assert quality["availability_enforced"] is True and quality["revision_count"] == 1
    assert quality["point_in_time_verified"] is False
    assert changed.with_config(job["config"]).context(later) == changed.context(later)


@pytest.mark.parametrize("market", ["stocks", "crypto"])
def test_frozen_vintage_research_exports_versions_and_enforces_synthetic_label(tmp_path, market):
    store, job, path, payload = versioned(tmp_path, market)
    study = register_study(store, job_ids=[job["id"]], card=CARD, protocol=PROTOCOL)
    assert study["plan"]["data_quality"]["data_kind"] == "synthetic"
    assert study["plan"]["data_quality"]["revision_count"] == 1
    path.write_text("the mutable source is no longer available")
    result = run_study(store, study["id"])
    assert result["report"]["execution_completed"]
    assert result["report"]["conclusion"] == "insufficient"
    assert result["input_snapshot"]["vintage_input"]["rows"][-1]["available_at"]
    assert len(result["input_snapshot"]["vintage_input"]["rows"]) == len(payload["rows"])
    with pytest.raises(ValueError, match="仅支持回测"):
        store.control(job["id"], "resume")


def test_vintage_basic_and_rolling_evaluations_preserve_availability(tmp_path):
    store, job, _, _ = versioned(tmp_path, "crypto")
    single = evaluate(store, job["id"], last_bars=4)
    rolling = walk_forward(store, [job["id"]], selection_bars=3, test_bars=3, folds=2, gap_bars=1)
    assert single["execution_completed"] and single["data_quality"]["availability_enforced"]
    assert rolling["data_quality"]["revision_count"] == 1


def test_invalid_timestamps_revisions_adjustments_and_missing_bars_are_rejected(tmp_path):
    store, job, path, original = versioned(tmp_path, "crypto")
    mutations = [
        (lambda p: p.update(price_basis="adjusted"), "原始未复权"),
        (lambda p: p["rows"][0].update(available_at="2025-01-02T00:15:00"), "带时区"),
        (lambda p: p["rows"].append(deepcopy(p["rows"][-1])), "相同发布时间"),
        (lambda p: p["rows"][0].update(available_at="2025-01-02T00:00:00+00:00"), "收盘和采集"),
        (lambda p: p["rows"][0].update(available_at="2025-01-02T00:16:00+00:00"), "迟发首版"),
        (lambda p: p["rows"][0].update(close=float("nan")), "finite|NaN"),
        (lambda p: p.update(captured_at=(datetime.now(UTC) + timedelta(days=1)).isoformat()), "未来"),
        (lambda p: p["rows"].pop(40), "连续"),
    ]
    for change, message in mutations:
        payload = deepcopy(original)
        change(payload)
        path.write_text(json.dumps(payload))
        with pytest.raises(ValueError, match=message):
            fetch_snapshot(job["spec"], job["config"], datetime.now(UTC), store.root, fresh=False)


def test_corporate_actions_cannot_disappear_during_registration(tmp_path):
    store, job, path, payload = versioned(tmp_path, "stocks")
    payload["actions"] = [{"symbol": "SPY", "date": payload["rows"][90]["date"],
                           "type": "split", "available_at": payload["rows"][89]["available_at"]}]
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="公司行动"):
        register_study(store, job_ids=[job["id"]], card=CARD, protocol=PROTOCOL)

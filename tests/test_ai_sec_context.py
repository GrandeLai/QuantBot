"""公告时点、版本冻结、模型契约与主回测/研究接入边界。"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import io
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from quantbot.ai.backtest import backtest
from quantbot.ai.feeds import fetch_snapshot
from quantbot.ai.models import OpenAICompatibleProvider, SYSTEM_PROMPT
from quantbot.ai.research import register_study, run_study
from quantbot.ai.sec_context import SECEventContext
from quantbot.cli import main
from quantbot.stocks.edgar.archive import EASTERN, SEC, parse_submission, quarters
from quantbot.stocks.edgar.event_example import example_submission
from quantbot.web.app import create_app
from tests.test_ai_research import CARD, PROTOCOL
from tests.test_ai_runtime import close_time, setup


def event_input(rows):
    filings = []
    for number, index in enumerate((70, 82, 90), 1):
        day = rows[index]["date"]
        raw_time = day.replace("-", "") + "120000"
        accession = f"0009999999-25-{number:06d}"
        identity = {"cik": "0001234567", "company": "DEMO SYNTHETIC", "form": "8-K", "filing_date": day,
                    "accession": accession, "source_url": f"{SEC}edgar/data/1234567/{accession}.txt"}
        seen = datetime.strptime(raw_time, "%Y%m%d%H%M%S").replace(tzinfo=EASTERN).astimezone(UTC) + timedelta(minutes=2)
        filings.append(parse_submission(example_submission(accession, raw_time, body=f"Synthetic event {number}."), identity, seen.isoformat()))
    record = filings[0]
    corrected_day = rows[85]["date"]
    corrected = example_submission(record["accession"], record["accepted_raw"], body="Corrected synthetic version.", changed=corrected_day.replace("-", ""))
    filings.append(parse_submission(corrected, {key: record[key] for key in ("cik", "company", "form", "filing_date", "accession", "source_url")}, corrected_day + "T18:00:00+00:00"))
    coverage = {"start": rows[0]["date"], "end": rows[-1]["date"], "ciks": ["0001234567"],
                "quarters": quarters(rows[0]["date"], rows[-1]["date"]), "complete": True,
                "universe_basis": "explicit_ciks_not_historical_population"}
    return {"schema_version": 1,
            "bundle": {"schema_version": 1, "data_kind": "synthetic", "source": "Explicit synthetic context test",
                       "captured_at": "2026-01-01T00:00:00+00:00", "coverage": coverage, "filings": filings},
            "universe": [{"cik": "0001234567", "symbol": "SPY", "start": rows[0]["date"], "end": rows[-1]["date"]}],
            "policy": {"lookback_days": 60}}


def with_events(tmp_path):
    store, base, prices, rows = setup(tmp_path, count=100)
    payload = event_input(rows)
    source = tmp_path / "sec-context.json"
    source.write_text(json.dumps(payload))
    job = store.create({**base["spec"], "sec_context_path": str(source)}, base["config"])
    return store, job, source, prices, rows, payload


def test_later_revisions_never_change_earlier_context_or_market_fills(tmp_path):
    store, job, source, _, rows, payload = with_events(tmp_path)
    original = fetch_snapshot(job["spec"], job["config"], datetime.now(UTC), store.root, fresh=False)
    before = original.context(close_time("stocks", rows[83]))
    after = original.context(close_time("stocks", rows[86]))
    assert "Synthetic event 1" in before.sec_events["symbols"]["SPY"]["events"][1]["text_excerpt"]
    revision = payload["bundle"]["filings"][-1]
    raw = revision["raw_submission"].replace("Corrected synthetic version.", "Different future correction.")
    payload["bundle"]["filings"][-1] = parse_submission(raw, {key: revision[key] for key in ("cik", "company", "form", "filing_date", "accession", "source_url")}, revision["first_seen_at"])
    source.write_text(json.dumps(payload))
    changed = fetch_snapshot(job["spec"], job["config"], datetime.now(UTC), store.root, fresh=False)
    assert changed.context(before.as_of) == before
    assert changed.context(after.as_of).snapshot_id != after.snapshot_id
    assert changed.context(before.as_of).prices == before.prices
    assert changed.bars(before.as_of, after.as_of) == original.bars(before.as_of, after.as_of)
    assert changed.with_config(job["config"]).context(before.as_of) == before


def test_cutoff_unknown_coverage_and_future_metadata_are_explicit(tmp_path):
    _, _, _, _, rows, payload = with_events(tmp_path)
    context = SECEventContext(payload, ["SPY", "QQQ"])
    first = payload["bundle"]["filings"][0]
    seen = datetime.fromisoformat(first["first_seen_at"])
    assert context.at(seen + timedelta(seconds=59), ["SPY"])["symbols"]["SPY"]["events"] == []
    visible = context.at(seen + timedelta(seconds=60), ["SPY", "QQQ"])
    assert len(visible["symbols"]["SPY"]["events"]) == 1
    assert visible["symbols"]["QQQ"]["mapping_available"] is False
    assert visible["point_in_time_verified"] is False
    early = context.at(close_time("stocks", rows[10]), ["SPY"])
    assert early["symbols"]["SPY"]["index_window_covered"] is False
    payload["policy"]["availability_mode"] = "acceptance_proxy"
    payload["bundle"]["filings"][0]["first_seen_at"] = rows[80]["date"] + "T18:00:00+00:00"
    proxy = SECEventContext(payload, ["SPY"]).at(close_time("stocks", rows[70]), ["SPY"])
    assert proxy["symbols"]["SPY"]["events"]
    assert "first_seen_at" not in proxy["symbols"]["SPY"]["events"][0]
    payload["policy"]["availability_mode"] = "declared"
    assert SECEventContext(payload, ["SPY"]).at(close_time("stocks", rows[90]), ["SPY"])["symbols"]["SPY"]["events"] == []


def test_limits_truncation_mapping_conflicts_and_invalid_policy(tmp_path):
    _, _, _, _, rows, payload = with_events(tmp_path)
    payload["policy"].update(max_events_per_symbol=1, max_text_chars=200)
    result = SECEventContext(payload, ["SPY"]).at(close_time("stocks", rows[91]), ["SPY"])["symbols"]["SPY"]
    assert result["visible_events_in_window"] == 3 and result["omitted_events"] == 2
    assert len(result["events"][0]["text_excerpt"]) <= 200
    for change in ({"lookback_days": True}, {"max_text_chars": 100000}, {"processing_delay_seconds": 0}, {"unknown": 1}):
        invalid = deepcopy(payload)
        invalid["policy"].update(change)
        with pytest.raises(ValueError):
            SECEventContext(invalid, ["SPY"])
    payload["universe"].append(deepcopy(payload["universe"][0]))
    with pytest.raises(ValueError, match="重叠"):
        SECEventContext(payload, ["SPY"])


def test_frozen_study_reuses_event_input_after_original_files_are_deleted(tmp_path):
    store, job, source, prices, _, payload = with_events(tmp_path)
    study = register_study(store, job_ids=[job["id"]], card=CARD, protocol=PROTOCOL)
    assert study["plan"]["data_quality"]["data_kind"] == "synthetic"
    assert study["plan"]["data_quality"]["sec_events"]["revision_versions"] == 1
    source.unlink()
    prices.unlink()
    result = run_study(store, study["id"])
    assert result["report"]["execution_completed"]
    assert result["report"]["conclusion"] == "insufficient"
    assert result["input_snapshot"]["sec_event_input"] == payload
    assert all("sec_events" in row for row in result["report"]["holdout"]["decisions"])
    with pytest.raises(ValueError, match="仅支持回测"):
        store.control(job["id"], "resume")


def test_versioned_prices_and_sec_events_can_be_replayed_together(tmp_path):
    store, job, _, _, rows, payload = with_events(tmp_path)
    from tests.test_ai_vintages import versioned

    (tmp_path / "vintage").mkdir()
    vintages_store, base, _, _ = versioned(tmp_path / "vintage", "stocks")
    # 复用同一时期的合成公告，与行情修订分别按自己的可得时间裁剪。
    combined = vintages_store.create({**base["spec"], "sec_context_path": job["spec"]["sec_context_path"]}, base["config"])
    snapshot = fetch_snapshot(combined["spec"], combined["config"], datetime.now(UTC), store.root, fresh=False)
    at = close_time("stocks", rows[86])
    assert snapshot.context(at).sec_events["symbols"]["SPY"]["events"]
    study = register_study(vintages_store, job_ids=[combined["id"]], card=CARD, protocol=PROTOCOL)
    result = run_study(vintages_store, study["id"])
    assert result["report"]["execution_completed"] and result["input_snapshot"]["sec_event_input"] == payload
    assert result["input_snapshot"]["vintage_input"]


def test_backtest_cli_web_and_download_include_actual_inputs_without_model_calls(tmp_path):
    _, job, source, prices, _, payload = with_events(tmp_path)
    config = job["config"]
    result = backtest(config, output=tmp_path / "direct", provider="demo", csv_data=prices.read_text(),
                      sec_context=payload, last_bars=10)
    assert not result["model_called"] and result["passed"]
    assert result["input_snapshot"]["sec_event_input"] == payload
    assert result["input_snapshot"]["rows"] and result["system_prompt"] == SYSTEM_PROMPT
    assert all("sec_events" in row for row in result["candidate"]["decisions"])
    client = TestClient(create_app(config=config, runtime_dir=tmp_path / "web"))
    response = client.post("/api/quantbot/allocate", json={"mode": "simulate", "provider": "demo", "last_bars": 10,
                           "csv_data": prices.read_text(), "sec_context": payload})
    assert response.status_code == 200, response.text
    assert response.json()["input_snapshot"] == result["input_snapshot"]
    stdout = io.StringIO()
    # CLI 默认特征需要 127 根；使用本测试显式配置。
    config_path = tmp_path / "config.toml"
    config_path.write_text("[strategy]\nmomentum_window=20\n")
    code = main(["backtest", "--provider", "demo", "--data", str(prices), "--sec-context", str(source),
                 "--config", str(config_path), "--last-bars", "10", "--output", str(tmp_path / "cli")], stdout=stdout, environ={})
    assert code == 0, stdout.getvalue()
    assert json.loads(stdout.getvalue())["input_snapshot"]["sec_event_input"] == payload


def test_actual_model_request_contract_contains_events_as_untrusted_data(tmp_path):
    store, job, _, _, rows, _ = with_events(tmp_path)
    context = fetch_snapshot(job["spec"], job["config"], datetime.now(UTC), store.root, fresh=False).context(close_time("stocks", rows[91]))
    from quantbot.ai.local_execution import model_account, new_account

    requests = []

    class Transport:
        def post_json(self, url, *, headers, payload):
            requests.append(payload)
            market = json.loads(payload["messages"][1]["content"])["market"]
            assert market["sec_events"] == context.sec_events
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                "snapshot_id": market["snapshot_id"], "as_of": market["as_of"], "decisions": [
                    {"symbol": "SPY", "target_weight": 0, "confidence": 1, "rationale": "controlled contract fixture"}]})}}]}

    config = deepcopy(job["config"])
    config["llm"].update(model="contract-fixture", base_url="http://127.0.0.1:9911/v1")
    provider = OpenAICompatibleProvider(config, environ={}, transport=Transport())
    decision = provider.decide(context, model_account(new_account(10000), context, "stocks"), config)
    assert len(requests) == 1 and requests[0]["messages"][0]["content"] == SYSTEM_PROMPT
    assert "untrusted evidence" in SYSTEM_PROMPT and decision.allocations[0].target_weight == 0


def test_unsupported_modes_invalid_files_and_authorization_reject_before_execution(tmp_path, monkeypatch):
    store, job, source, _, _, payload = with_events(tmp_path)
    monkeypatch.setattr("quantbot.ai.evaluation.provider_for", lambda *a: pytest.fail("must not construct provider"))
    with pytest.raises(ValueError, match="允许模型调用"):
        backtest(job["config"], output=tmp_path / "unauthorized", sec_context=payload)
    assert not (tmp_path / "unauthorized").exists()
    client = TestClient(create_app(config=job["config"], runtime_dir=tmp_path / "web"))
    for market, mode in (("stocks", "decide"), ("stocks", "paper"), ("crypto", "simulate")):
        response = client.post("/api/quantbot/allocate", json={"market": market, "mode": mode, "provider": "demo", "sec_context": payload})
        assert response.status_code == 400 and "仅支持美股历史回测" in response.json()["detail"]
    source.write_text("null")
    with pytest.raises(ValueError, match="AI 公告输入"):
        fetch_snapshot(job["spec"], job["config"], datetime.now(UTC), store.root, fresh=False)
    with pytest.raises(ValueError, match="仅支持回测"):
        fetch_snapshot(job["spec"], job["config"], datetime.now(UTC), store.root)


def test_different_event_sources_cannot_share_a_candidate_comparison(tmp_path):
    store, job, source, _, _, _ = with_events(tmp_path)
    other = store.create({**job["spec"], "sec_context_path": str(source.with_name("other.json"))}, job["config"])
    with pytest.raises(ValueError, match="相同标的"):
        register_study(store, job_ids=[job["id"], other["id"]], card=CARD, protocol=PROTOCOL)
    from quantbot.ai.evaluation import walk_forward

    with pytest.raises(ValueError, match="相同 SEC"):
        walk_forward(store, [job["id"], other["id"]])


def test_frozen_event_tampering_is_detected_before_provider_calls(tmp_path, monkeypatch):
    store, job, _, _, _, _ = with_events(tmp_path)
    study = register_study(store, job_ids=[job["id"]], card=CARD, protocol=PROTOCOL)
    snapshot = Path(study["plan"]["snapshot"]["path"])
    payload = json.loads(snapshot.read_text())
    payload["sec_event_input"]["policy"]["lookback_days"] = 30
    snapshot.write_text(json.dumps(payload))
    monkeypatch.setattr("quantbot.ai.research.provider_for", lambda *a: pytest.fail("must not call provider"))
    with pytest.raises(ValueError, match="行情指纹"):
        run_study(store, study["id"])

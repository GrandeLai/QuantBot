from copy import deepcopy
from datetime import UTC, date, datetime
import io
import json

from fastapi.testclient import TestClient
import httpx
import pytest

from quantbot.ai.config import load_config
from quantbot.cli import main
from quantbot.stocks.edgar.archive import EdgarArchive, SEC, archive_edgar, parse_master, parse_submission, quarters
from quantbot.stocks.edgar.event_data import EventPrices, available_at, load_event_bundle, visible_events
from quantbot.stocks.edgar.event_example import example_input, example_submission
from quantbot.stocks.edgar.event_study import EventPolicy, event_study, window_return
from quantbot.web.app import create_app


def identity(accession="0009999999-24-000001", filed="2024-12-31", form="8-K"):
    return {"cik": "0001234567", "company": "DEMO SYNTHETIC", "form": form, "filing_date": filed,
            "accession": accession, "source_url": f"{SEC}edgar/data/1234567/{accession}.txt"}


def master(rows):
    return "Description: Master Index\nCIK|Company Name|Form Type|Date Filed|Filename\n---------------------\n" + "\n".join(
        f"1234567|DEMO SYNTHETIC|{r['form']}|{r['filing_date']}|edgar/data/1234567/{r['accession']}.txt" for r in rows)


def test_quarter_rollover_includes_both_years_and_exact_dates():
    q = quarters("2024-12-31", "2025-01-02")
    assert [(x["year"], x["quarter"]) for x in q] == [(2024, 4), (2025, 1)]
    old, new = identity(), identity("0009999999-25-000002", "2025-01-02")
    rows = parse_master(master([old, new, old]), date(2024, 12, 31), date(2025, 1, 2), {"0001234567"})
    assert rows == [old, new]
    assert parse_master(master([old]), date(2025, 1, 1), date(2025, 1, 2), {"0001234567"}) == []
    with pytest.raises(ValueError, match="8 个季度"):
        quarters("2020-01-01", "2025-01-01")
    with pytest.raises(ValueError, match="不是有效"):
        parse_master("<html>Access denied</html>", date(2024, 1, 1), date(2025, 1, 1), {"0001234567"})
    with pytest.raises(ValueError, match="路径"):
        parse_master(master([old]).replace("edgar/data/1234567/", "https://example.com/"), date(2024, 1, 1), date(2025, 1, 1), {"0001234567"})


@pytest.mark.parametrize("stamp,utc", [("20241231160000", "2024-12-31T21:00:00+00:00"), ("20240701160000", "2024-07-01T20:00:00+00:00")])
def test_sgml_acceptance_uses_eastern_dst_and_exact_primary_document(stamp, utc):
    filed = datetime.strptime(stamp[:8], "%Y%m%d").date().isoformat()
    row = identity(filed=filed)
    raw = example_submission(row["accession"], stamp)
    result = parse_submission(raw, row, "2025-02-01T00:00:00+00:00")
    assert result["accepted_at"] == utc
    assert result["primary_document"] == "main.htm" and "Exhibit, not the main" not in result["text"]
    assert result["items"] == ["2.02", "9.01"] and result["public_available_at"] is None
    assert available_at(result, "observed", 900) == datetime(2025, 2, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="身份"):
        parse_submission(raw.replace("CONFORMED SUBMISSION TYPE: 8-K", "CONFORMED SUBMISSION TYPE: 4"), row, "2025-02-01T00:00:00+00:00")


def test_archive_repeats_idempotently_preserves_versions_and_partial_failure(tmp_path, monkeypatch):
    rows = [identity(), identity("0009999999-25-000002", "2025-01-02")]
    documents = {rows[0]["source_url"]: example_submission(rows[0]["accession"], "20241231150000"),
                 rows[1]["source_url"]: example_submission(rows[1]["accession"], "20250102110000")}
    calls, fails = [], set()
    real_client = httpx.Client

    def handler(request):
        url = str(request.url)
        calls.append(url)
        assert request.headers["User-Agent"] == "QuantBot test@example.invalid"
        if url in fails:
            return httpx.Response(503, text="temporarily unavailable")
        if url.endswith("QTR4/master.idx"):
            return httpx.Response(200, text=master(rows[:1]))
        if url.endswith("QTR1/master.idx"):
            return httpx.Response(200, text=master(rows[1:]))
        return httpx.Response(200, text=documents[url])

    monkeypatch.setattr("quantbot.stocks.edgar.archive.httpx.Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr("quantbot.stocks.edgar.archive.time.sleep", lambda seconds: None)
    args = {"directory": tmp_path / "archive", "start": "2024-12-31", "end": "2025-01-02", "ciks": ["1234567"], "user_agent": "QuantBot test@example.invalid"}
    first = archive_edgar(**args)
    assert first["status"] == "completed" and len(calls) == 4
    original = json.loads(open(first["bundle_path"]).read())
    again = archive_edgar(**args)
    repeated = json.loads(open(again["bundle_path"]).read())
    assert original["filings"] == repeated["filings"] and len(repeated["filings"]) == 2
    documents[rows[0]["source_url"]] = example_submission(rows[0]["accession"], "20241231150000", body="Corrected body", changed="20250201")
    updated = archive_edgar(**args)
    versions = json.loads(open(updated["bundle_path"]).read())["filings"]
    assert len(versions) == 3 and len({v["content_hash"] for v in versions if v["accession"] == rows[0]["accession"]}) == 2
    fails.add(rows[1]["source_url"])
    failed = archive_edgar(**args)
    assert failed["status"] == "failed" and failed["errors"][0]["accession"] == rows[1]["accession"]
    partial = json.loads(open(failed["bundle_path"]).read())
    with pytest.raises(ValueError, match="未完整"):
        load_event_bundle(partial)
    store = EdgarArchive(args["directory"])
    assert len(store.versions([r["accession"] for r in rows])) == 3
    assert all(v["public_available_at"] is None for v in versions)
    with pytest.raises(ValueError, match="User-Agent"):
        archive_edgar(**{**args, "user_agent": ""})


def test_visible_version_does_not_rewrite_history_or_count_amendment_twice():
    sample = example_input()
    versions, duplicates = load_event_bundle(sample["bundle"])
    assert duplicates == 1 and len(versions) == 9
    accession = sample["bundle"]["filings"][0]["accession"]
    before = next(r for r in visible_events(versions, "2025-01-30T22:00:00+00:00") if r["accession"] == accession)
    after = next(r for r in visible_events(versions, "2025-02-04T22:00:00+00:00") if r["accession"] == accession)
    assert "Later corrected" not in before["text"] and "Later corrected" in after["text"]
    assert before["content_hash"] != after["content_hash"]
    assert available_at(after, "acceptance_proxy", 900, first_version=False) == datetime(2025, 2, 3, 14, tzinfo=UTC)


def test_event_windows_obey_release_clock_holidays_and_overlap(tmp_path):
    result = event_study(**example_input(), output=tmp_path / "report")
    assert result["execution_completed"] and result["research_conclusion"] == "insufficient"
    events = {e["accessions"][0]: e for e in result["events"]}
    assert events["0009999999-24-000001"]["entry_date"] == "2024-12-06"  # 盘前
    assert events["0009999999-24-000002"]["entry_date"] == "2024-12-30"  # 周五盘后
    assert events["0009999999-25-000005"]["entry_date"] == "2025-01-21"  # MLK 休市
    assert events["0009999999-25-000007"]["entry_at"] == "2025-03-10T13:30:00+00:00"  # DST
    assert any(s["reason"] == "amendment_excluded" for s in result["skipped"])
    assert any(s["reason"] == "overlapping_event_window" for s in result["skipped"])
    assert any(s["reason"] == "incomplete_forward_window" for s in result["skipped"])
    assert all(datetime.fromisoformat(e["entry_at"]) > datetime.fromisoformat(e["available_at"]) for e in result["events"])
    assert result["data_quality"]["revision_versions"] == result["data_quality"]["duplicate_records"] == 1
    rerun = event_study(**example_input(), output=tmp_path / "rerun")
    assert result["events"] == rerun["events"] and result["summary"] == rerun["summary"]


def test_later_download_is_not_historical_public_availability_without_opt_in(tmp_path):
    sample = example_input()
    for record in sample["bundle"]["filings"]:
        record["first_seen_at"] = "2025-04-01T00:00:00+00:00"
    # 同 accession 的修订必须仍有独立、晚到的首次取得时间。
    sample["bundle"]["filings"][-2]["first_seen_at"] = "2025-04-01T01:00:00+00:00"
    observed = event_study(**sample, output=tmp_path / "observed")
    assert not observed["events"] and observed["research_conclusion"] == "insufficient"
    sample["policy"]["availability_mode"] = "acceptance_proxy"
    assumed = event_study(**sample, output=tmp_path / "proxy")
    assert assumed["events"] and assumed["data_quality"]["point_in_time_verified"] is False
    cross_quarter = sample["bundle"]["filings"][2]
    assert available_at(cross_quarter, "acceptance_proxy", 900) == datetime(2025, 1, 2, 5, tzinfo=UTC)


def test_declared_release_requires_evidence_and_consistent_raw_metadata(tmp_path):
    sample = example_input()
    record = sample["bundle"]["filings"][0]
    record["accepted_at"] = "2020-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="原始文件"):
        load_event_bundle(sample["bundle"])
    sample = example_input()
    sample["bundle"]["filings"][0]["public_available_at"] = "2024-12-06T13:01:00+00:00"
    with pytest.raises(ValueError, match="证据来源"):
        load_event_bundle(sample["bundle"])
    sample = example_input()
    sample["policy"]["availability_mode"] = "declared"
    result = event_study(**sample, output=tmp_path / "declared")
    assert not result["events"] and any(s["reason"] == "public_availability_unknown" for s in result["skipped"])


def test_return_accounting_includes_later_dividend_and_both_sides_cost():
    sample = example_input()["prices"]
    days = sorted({r["date"] for r in sample["bars"]})[:3]
    sample["bars"] = [r for r in sample["bars"] if r["date"] in days]
    for row in sample["bars"]:
        row.update(open=100, high=111, low=99, close=110)
    sample["dividends"] = [{"symbol": "DEMO", "ex_date": days[0], "amount": 50}, {"symbol": "DEMO", "ex_date": days[1], "amount": 2}]
    data = EventPrices(sample)
    assert window_return(data, "DEMO", 0, 1, 10) == pytest.approx((110 * 0.999 + 2) / (100 * 1.001) - 1)
    assert window_return(data, "DEMO", 0, 0, 0) == pytest.approx(0.1)  # 买入当日除息不获分红。


def test_same_symbol_same_open_is_one_event_group(tmp_path):
    sample = example_input()
    row = deepcopy(sample["bundle"]["filings"][0])
    second = identity("0009999999-24-000020", "2024-12-06")
    sample["bundle"]["filings"].append(parse_submission(example_submission(second["accession"], "20241206081000"), second, "2024-12-06T13:12:00+00:00"))
    result = event_study(**sample, output=tmp_path / "grouped")
    assert len(result["events"][0]["accessions"]) == 2
    assert row["accession"] in result["events"][0]["accessions"]


def test_event_received_at_open_waits_for_next_open_and_declared_time_is_opt_in(tmp_path):
    sample = example_input()
    row = identity(filed="2024-12-06")
    record = parse_submission(example_submission(row["accession"], "20241206092900"), row, "2024-12-06T14:30:00+00:00")
    sample["bundle"]["filings"] = [record]
    result = event_study(**sample, output=tmp_path / "after-open")
    assert result["events"][0]["entry_date"] == "2024-12-09"
    record["public_available_at"] = "2024-12-06T14:29:00+00:00"
    record["availability_evidence"] = "synthetic feed timestamp"
    sample["policy"]["availability_mode"] = "declared"
    declared = event_study(**sample, output=tmp_path / "declared")
    assert declared["events"][0]["entry_date"] == "2024-12-06"


def test_post_acceptance_correction_cannot_claim_original_release_time():
    sample = example_input()
    correction = sample["bundle"]["filings"][-2]
    correction["public_available_at"] = correction["accepted_at"]
    correction["availability_evidence"] = "invalid backdated claim"
    with pytest.raises(ValueError, match="公开时间"):
        load_event_bundle(sample["bundle"])


def test_window_start_keeps_weekend_event_but_never_clamps_an_older_trade(tmp_path):
    sample = example_input()
    sample["prices"]["bars"] = [r for r in sample["prices"]["bars"] if r["date"] >= "2024-12-09"]
    row = identity(filed="2024-12-06")
    sample["bundle"]["filings"] = [parse_submission(example_submission(row["accession"], "20241206170000"), row, "2024-12-06T22:02:00+00:00")]
    result = event_study(**sample, output=tmp_path / "weekend")
    assert result["events"][0]["entry_date"] == "2024-12-09"
    sample["bundle"]["filings"] = [parse_submission(example_submission(row["accession"], "20241206080000"), row, "2024-12-06T13:02:00+00:00")]
    earlier = event_study(**sample, output=tmp_path / "missing-open")
    assert not earlier["events"] and earlier["skipped"][0]["reason"] == "outside_price_window"


@pytest.mark.parametrize("changes", [{"horizons": [5, 1]}, {"horizons": [True]}, {"order_latency_seconds": 0}, {"cost_bps_per_side": float("nan")}, {"availability_mode": "automatic"}])
def test_policy_rejects_ambiguous_parameters(changes):
    with pytest.raises(ValueError):
        EventPolicy(**changes)


def test_cli_web_and_download_reproduce_same_event_results(tmp_path):
    sample = example_input()
    for name, payload in sample.items():
        (tmp_path / f"{name}.json").write_text(json.dumps(payload))
    code = main(["sec-study", "--events", str(tmp_path / "bundle.json"), "--prices", str(tmp_path / "prices.json"),
                 "--universe", str(tmp_path / "universe.json"), "--policy", str(tmp_path / "policy.json"),
                 "--output", str(tmp_path / "cli")], stdout=io.StringIO(), environ={})
    assert code == 0
    cli = json.loads((tmp_path / "cli" / "report.json").read_text())
    client = TestClient(create_app(config=load_config(environ={}), runtime_dir=tmp_path / "web"))
    response = client.post("/api/quantbot/sec-study", json=sample)
    assert response.status_code == 200 and "event_input" not in response.json()
    downloaded = client.get(response.json()["download_url"]).json()
    assert downloaded["events"] == cli["events"] and downloaded["summary"] == cli["summary"]
    assert downloaded["event_input"] == sample["bundle"] and downloaded["price_input"] == sample["prices"]
    assert downloaded["model_calls"] == downloaded["broker_calls"] == 0
    assert client.get("/api/quantbot/sec-study/reports/invalid").status_code == 404
    assert client.post("/api/quantbot/sec-study", json={**sample, "live": True}).status_code == 422
    sample["prices"]["bars"][0]["date"] = None
    (tmp_path / "prices.json").write_text(json.dumps(sample["prices"]))
    assert main(["sec-study", "--events", str(tmp_path / "bundle.json"), "--prices", str(tmp_path / "prices.json"),
                 "--universe", str(tmp_path / "universe.json"), "--output", str(tmp_path / "invalid")], stdout=io.StringIO(), environ={}) == 2
    assert not (tmp_path / "invalid").exists()

from pathlib import Path

import pytest

from quantbot.ai import evaluation
from quantbot.ai.feeds import fetch_snapshot
from quantbot.ai.models import DemoProvider
from quantbot.ai.replay_evidence import inspect_replay
from tests.test_ai_runtime import close_time, setup


def assert_engineering_only(report, expected=True):
    assert report["engineering_passed"] is expected
    assert report["broker_paper_authorized"] is False
    if "passed" in report:
        assert report["passed"] is expected


def test_engineering_check_compatibility_does_not_measure_profitability():
    result = {"equity_curve": [{}, {}, {}], "decisions": [{}, {}], "errors": [],
              "risk_hold": None, "return_fraction": -0.02}
    assert all(evaluation.engineering_checks(result, 3).values())
    assert evaluation.admission_checks(result, 3) == evaluation.engineering_checks(result, 3)
    assert not evaluation.engineering_checks(result, 4)["complete_window"]


def test_execution_wait_counts_as_handled_bar_but_does_not_bypass_risk():
    result = {"equity_curve": [{}, {}, {}, {}], "decisions": [{}, {}], "errors": [],
              "risk_hold": None, "execution_waits": [{"as_of": "2025-01-03T00:00:00+00:00"}]}
    assert all(evaluation.engineering_checks(result, 4).values())
    result["execution_waits"] = []
    assert not evaluation.engineering_checks(result, 4)["complete_decisions"]
    result["execution_waits"] = [{}]
    result["risk_hold"] = "drawdown_limit"
    assert not evaluation.engineering_checks(result, 4)["no_risk_hold"]


@pytest.mark.parametrize("market", ["crypto", "stocks"])
@pytest.mark.parametrize("kind", ["evaluate", "walk_forward"])
def test_evaluation_outputs_and_journals_distinguish_engineering_from_authority(
    tmp_path, monkeypatch, market, kind
):
    store, job, _, _ = setup(tmp_path, market, count=100)

    def obsolete_name(*args):
        pytest.fail("Internal evaluation paths must use engineering_checks")

    monkeypatch.setattr(evaluation, "admission_checks", obsolete_name)
    if kind == "evaluate":
        output = evaluation.evaluate(store, job["id"], last_bars=4)
    else:
        output = evaluation.walk_forward(store, [job["id"]], selection_bars=3, test_bars=3, folds=2)
        for row in output["comparisons"] + output["evaluations"]:
            assert_engineering_only(row)
        for fold in output["folds"]:
            for score in fold["selection_scores"]:
                assert_engineering_only(score)
    assert_engineering_only(output)
    assert store.get(job["id"])["stage"] == "evaluated"
    saved = store.history(job["id"])["evaluations"][0]["report"]
    assert_engineering_only(saved)
    if kind == "evaluate":
        replays = [saved["candidate"], saved["baseline"]]
    else:
        replays = []
        for fold in saved["folds"]:
            assert_engineering_only(fold)
            replays.extend(fold[name] for name in ("selection", "candidate", "baseline"))
    for result in replays:
        assert_engineering_only(result)
        journal = inspect_replay(Path(result["journal_path"]))
        assert_engineering_only(journal["records"][-1]["payload"])


def test_replay_without_journal_also_has_no_broker_paper_authority(tmp_path):
    store, job, _, rows = setup(tmp_path, "crypto")
    snapshot = fetch_snapshot(job["spec"], job["config"], close_time("crypto", rows[-1]), store.root)
    result = evaluation.replay(snapshot, job["spec"], job["config"], DemoProvider(), last_bars=4)
    assert_engineering_only(result)

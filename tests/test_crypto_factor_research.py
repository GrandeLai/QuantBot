from datetime import UTC, datetime
from io import StringIO
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from quantbot.backtest import compute_ic_series, evaluate_alpha_subset_acceptance, summarize_ic
from quantbot.cli import main
from quantbot.experiment_registry import ExperimentRegistry
from quantbot.factors.crypto_panel import (
    DAY, FACTORS, build_labels, build_panel, load_daily_data,
)
from quantbot.factors.evaluation import FactorMetrics, passes_admission, pearson_ic
from quantbot.factors.study import register_study, run_study
from quantbot.factors.time_series import (
    PROTOCOL, _deduplicate, _fold_statistics, _screen, block_evidence, evaluate_panel,
    holm_adjust, hypotheses, rank_ic, study_protocol,
)
from quantbot.integrations.rdagent_runtime import RDAgentQlibAdapter
from quantbot.runtime_factory import _RDAgentSmokeExperimentFactory
from quantbot.runtime_smoke import RuntimeSmokeSuite


def daily_data(days=120):
    rng = np.random.default_rng(42)
    dates = pd.date_range("2023-01-01", periods=days, tz="UTC")
    records = []
    for symbol, initial in (("BTC/USDT", 20000), ("ETH/USDT", 1500)):
        close = initial * np.exp(np.cumsum(rng.normal(0.001, 0.02, days)))
        for i, stamp in enumerate(dates):
            opening = close[i] * 0.99
            records.append({"timestamp": stamp, "as_of": stamp+DAY, "symbol": symbol, "bar": "1d",
                            "open": opening, "high": close[i]*1.01, "low": opening*0.99,
                            "close": close[i], "volume": 1000+i})
    return pd.DataFrame(records)


def write_data(tmp_path, frame=None):
    frame = daily_data() if frame is None else frame
    path = tmp_path / "daily.csv"
    frame.drop(columns="as_of").to_csv(path, index=False)
    return path


def test_legacy_ic_unavailable_is_not_zero_or_infinity():
    assert pearson_ic({"BTC": 1, "ETH": 2}, {"BTC": 0.1, "ETH": 0.2}) is None
    assert pearson_ic(dict(A=1, B=1, C=1), dict(A=1, B=2, C=3)) is None
    assert pearson_ic(dict(A=1, B=2, C=float("nan")), dict(A=1, B=2, C=3)) is None
    summary = summarize_ic("short", {1: 0.8})
    assert summary.icir is None and summary.observations == 1
    constant = summarize_ic("constant", {1: 0.8, 2: 0.8})
    assert constant.status == "insufficient_evidence" and constant.icir is None
    missing = summarize_ic("pair", compute_ic_series({1: {"A": 1, "B": 2}}, {1: {"A": 1, "B": 3}}))
    assert missing.ic_mean is None and missing.missing_observations == 1
    report = evaluate_alpha_subset_acceptance([constant], [], min_factor_count=1)
    assert not report.passed and "insufficient_ic_evidence" in report.reasons


@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_legacy_factor_gate_rejects_nonfinite_evidence(value):
    assert not passes_admission(FactorMetrics("bad", 0.03, value, 0.6, 0.1, 4, 0.03))


def test_rd_agent_missing_statistics_never_get_passing_defaults():
    adapter = RDAgentQlibAdapter(lambda **kwargs: SimpleNamespace(
        run=lambda: SimpleNamespace(ic_mean=0.03, icir=0.5, accepted=True)
    ))
    result = adapter.run_factor_experiment("x", "us_sp500", ("2020", "2021"))
    assert not result.accepted and result.metrics is None and "oos_ic" in result.error
    assert not passes_admission(result.metrics)


def test_rd_agent_smoke_constructs_but_never_runs_research():
    class Experiment:
        def __init__(self, sub_tasks):
            assert sub_tasks == []

        def run(self):
            raise AssertionError("research/model invocation during smoke")

    suite = RuntimeSmokeSuite(None, None, None, None, _RDAgentSmokeExperimentFactory(Experiment), None, None)
    assert "construction_only" in suite._smoke_rd_agent()


def test_registry_retains_all_factor_diagnostics_without_expanding_strategy_cap():
    registry = ExperimentRegistry()
    args = {"experiment_id": "factor", "preregistered_at": datetime.now(UTC)}
    record = registry.register_factor_screen(**args, hypothesis_hashes=[str(i) for i in range(72)])
    for trial_id in record.trial_ids:
        registry.record_trial("factor", trial_id)
    assert registry.total_declared_trials == registry.completed_trial_count == 72
    with pytest.raises(ValueError, match="72"):
        registry.register_factor_screen(**{**args, "experiment_id": "large"},
                                        hypothesis_hashes=[str(i) for i in range(73)])
    with pytest.raises(ValueError, match="six"):
        registry.register_candidates(**{**args, "experiment_id": "strategies"}, market="crypto",
                                     candidate_hashes=[str(i) for i in range(7)])


def test_factor_definitions_and_symbol_horizon_ledger_are_finite():
    assert len(FACTORS) == len({item.to_dict()["definition_hash"] for item in FACTORS}) == 12
    assert len(hypotheses()) == 72
    assert sum(item["inferential"] for item in hypotheses()) == 51


def test_factor_panel_has_no_future_dependencies_and_records_missing_history():
    data = daily_data(150)
    full = build_panel(data)
    cutoff = pd.Timestamp("2023-04-11", tz="UTC")
    prefix = build_panel(data[data.as_of <= cutoff])
    pd.testing.assert_frame_equal(prefix.reset_index(drop=True), full[full.as_of <= cutoff].reset_index(drop=True))
    first = full[full.as_of == full.as_of.min()]
    assert first[first.factor == "momentum_20"].missing_reason.eq("insufficient_history").all()
    residual = full[(full.factor == "eth_residual_20") & (full.symbol == "ETH/USDT")]
    assert residual.iloc[79].missing_reason == "insufficient_history"
    assert pd.notna(residual.iloc[80].value)
    assert full[(full.factor == "eth_residual_20") & (full.symbol == "BTC/USDT")].value.isna().all()
    relative = full[full.factor == "relative_momentum_20"].pivot(index="as_of", columns="symbol", values="value")
    np.testing.assert_allclose(relative.sum(axis=1), 0, atol=1e-12)


def test_breakout_uses_prior_highs_and_residual_fit_excludes_scoring_window():
    data = daily_data()
    panel = build_panel(data)
    btc = data[data.symbol == "BTC/USDT"].reset_index(drop=True)
    eth = data[data.symbol == "ETH/USDT"].reset_index(drop=True)
    t = 100
    x, y = np.diff(np.log(btc.close)), np.diff(np.log(eth.close))
    beta, alpha = np.polyfit(x[t-80:t-20], y[t-80:t-20], 1)
    expected = float(np.sum(y[t-20:t] - alpha - beta*x[t-20:t]))
    actual = panel[(panel.symbol == "ETH/USDT") & (panel.factor == "eth_residual_20")].iloc[t].value
    assert actual == pytest.approx(expected)
    changed = data.copy()
    changed.loc[(changed.symbol == "BTC/USDT") & (changed.timestamp == btc.timestamp.iat[t]), "high"] *= 10
    before = panel[(panel.symbol == "BTC/USDT") & (panel.factor == "breakout_20")].iloc[t].value
    after = build_panel(changed)
    after_value = after[(after.symbol == "BTC/USDT") & (after.factor == "breakout_20")].iloc[t].value
    # Current high can affect ATR, but never enters the previous-20-high numerator.
    assert np.sign(after_value) == np.sign(before) and abs(after_value) < abs(before)


def test_fixed_horizon_labels_use_next_opens_and_both_sides_of_cost():
    data = daily_data(30)
    btc = data[data.symbol == "BTC/USDT"].reset_index(drop=True)
    labels = build_labels(data)
    label = labels[(labels.symbol == "BTC/USDT") & (labels.horizon == 5)].iloc[4]
    ratio = btc.open.iat[10] / btc.open.iat[5]
    assert label.entry_at == btc.timestamp.iat[5]
    assert label.exit_at == btc.timestamp.iat[10]
    assert label.gross_return == pytest.approx(ratio-1)
    assert label.net_return == pytest.approx(ratio*0.9985/1.0015-1)
    assert label.stress_return == pytest.approx(ratio*0.997/1.003-1)
    other = labels[(labels.symbol == "ETH/USDT") & (labels.horizon == 5)].iloc[4]
    assert label.relative_gross_return == pytest.approx(label.gross_return-other.gross_return)
    assert other.relative_gross_return == pytest.approx(-label.relative_gross_return)
    assert labels[labels.as_of == labels.as_of.max()].missing_reason.eq("missing_future").all()
    assert labels[labels.as_of == labels.as_of.max()].net_return.isna().all()


def test_zero_volume_and_zero_variance_are_missing_not_normal_scores():
    data = daily_data(30)
    data.loc[:, ["open", "high", "low", "close"]] = 100
    data.loc[:, "volume"] = 0
    panel = build_panel(data)
    last = panel[panel.as_of == panel.as_of.max()]
    assert last[last.factor == "illiquidity_20"].missing_reason.eq("zero_volume").all()
    assert last[last.factor == "close_location"].value.isna().all()
    labels = build_labels(data)
    assert labels[labels.as_of == labels.as_of.min()].missing_reason.eq("zero_volume_execution_bar").all()


@pytest.mark.parametrize("defect", ["future", "duplicate", "gap", "invalid_ohlc", "timezone", "nan"])
def test_data_audit_rejects_unsafe_history(tmp_path, defect):
    data = daily_data(30)
    end = "2023-01-31T00:00:00Z"
    if defect == "future":
        end = "2023-01-30T00:00:00Z"
    elif defect == "duplicate":
        data = pd.concat([data, data.iloc[[0]]])
    elif defect == "gap":
        data = data.drop(index=10)
    elif defect == "invalid_ohlc":
        data.loc[0, "high"] = 1
    elif defect == "timezone":
        data["timestamp"] = data.timestamp.dt.tz_localize(None)
    else:
        data.loc[0, "close"] = np.nan
    with pytest.raises(ValueError):
        load_daily_data(write_data(tmp_path, data), end, "2023-02-02T00:00:00Z")


def test_training_labels_are_purged_and_validation_cannot_change_thresholds():
    dates = pd.date_range("2020-01-01", periods=600, tz="UTC")
    frame = pd.DataFrame({"as_of": dates, "exit_at": dates + 10*DAY, "value": np.arange(600),
                          "net_return": np.sin(np.arange(600))/100, "gross_return": np.sin(np.arange(600))/100,
                          "relative_gross_return": np.cos(np.arange(600))/100,
                          "stress_return": np.sin(np.arange(600))/100-0.003})
    start, end = dates[400], dates[580]
    before, *_ = _fold_statistics(frame, start, end, 10, start)
    assert pd.Timestamp(before["training_last_label_exit"]) < start-DAY
    assert before["training_observations"] == 389
    frame.loc[frame.as_of >= start, "value"] += 100000
    after, *_ = _fold_statistics(frame, start, end, 10, start)
    assert before["entry_threshold"] == after["entry_threshold"]
    assert before["train_rank_ic"] == after["train_rank_ic"]


def test_positive_market_return_alone_is_not_supported_factor_lift():
    evidence = block_evidence(np.full(120, 0.01), np.arange(120) % 3 == 0, 1, replicates=199)
    assert evidence["p_value"] > 0.05
    assert evidence["lift_ci_low"] <= 1e-12
    assert rank_ic([1, 1, 1], [1, 2, 3]) is None
    assert holm_adjust([0.001, 0.03, 0.04]) == pytest.approx([0.003, 0.06, 0.06])


def test_screen_can_accept_supported_effect_but_refuses_insufficient_events():
    # Deliberately strong synthetic effect verifies the positive gate path only.
    returns = np.tile([-0.01, 0.04], 270)
    active = returns > 0
    result = {"horizon": 1, "status": "rejected", "reasons": [], "folds": [
        {"reasons": [], "train_rank_ic": 0.5, "rank_ic": 0.5,
         "mean_net_event_return": 0.04, "selected_events": 90} for _ in range(3)
    ]}
    _screen(result, returns, active, returns-0.003, 1200)
    assert result["reasons"] == []
    assert result["bootstrap"]["net_ci_low"] > 0
    assert result["bootstrap"]["lift_ci_low"] > 0
    assert holm_adjust([result["p_value"]] + [1.0]*50)[0] < 0.05
    result["reasons"] = []
    for fold in result["folds"]:
        fold["selected_events"] = 1
    _screen(result, returns, np.arange(540) < 3, returns-0.003, 1200)
    assert result["status"] == "insufficient_evidence"
    assert "insufficient_nonoverlapping_events" in result["reasons"]


def test_pool_deduplication_uses_training_quality_and_retains_failed_alternatives():
    dates = pd.date_range("2020-01-01", periods=20, tz="UTC")
    panel = pd.DataFrame([{"as_of": stamp, "symbol": "BTC/USDT", "factor": factor, "value": i}
                          for factor in ("momentum_20", "momentum_60") for i, stamp in enumerate(dates)])
    results = [{"key": factor, "symbol": "BTC/USDT", "factor": factor,
                "status": "eligible_development_factor", "folds": [{"train_rank_ic": quality}]}
               for factor, quality in (("momentum_20", 0.2), ("momentum_60", 0.4))]
    pool, correlations = _deduplicate(results, panel, dates[-1]+DAY)
    assert pool == ["momentum_60"]
    assert results[0]["status"] == "redundant_development_factor"
    assert len(results) == 2 and correlations[0]["rank_correlation"] == pytest.approx(1.0)


def test_cli_freezes_and_keeps_an_empty_pool_for_insufficient_data(tmp_path):
    data = write_data(tmp_path)
    stream = StringIO()
    assert main(["factor-study", "register", "--data", str(data), "--root", str(tmp_path/"family"),
                 "--development-end", "2023-05-01T00:00:00Z",
                 "--protected-holdout-start", "2023-05-03T00:00:00Z"], stdout=stream) == 0
    from pathlib import Path
    study = Path(json.loads(stream.getvalue())["study"])
    output = StringIO()
    assert main(["factor-study", "run", "--study", str(study)], stdout=output) == 0
    result = json.loads((study/"result.json").read_text())
    assert result["pool"] == [] and result["declared_trials"] == 72
    assert result["inferential_hypotheses"] == 51
    assert all(row["status"] in {"insufficient_evidence", "not_applicable"} for row in result["results"])
    assert result["model_calls"] == 0 and not result["holdout_evaluated"] and not result["trading_admission"]
    assert main(["factor-study", "run", "--study", str(study)], stdout=StringIO()) == 2
    assert not (study/"failure.json").exists()  # Repeat refusal does not mutate the completed study.


def test_changed_frozen_data_is_rejected_and_failure_is_retained(tmp_path):
    study = register_study(tmp_path/"family", write_data(tmp_path), development_end="2023-05-01T00:00:00Z",
                           protected_holdout_start="2023-05-03T00:00:00Z")
    with (study/"data.csv").open("a") as handle:
        handle.write("\n")
    with pytest.raises(ValueError, match="frozen input changed"):
        run_study(study)
    assert (study/"failure.json").exists()
    assert not (study/"result.json").exists()
    ledger = [json.loads(line) for line in (study.parent/"ledger.jsonl").read_text().splitlines()]
    assert [row["event"] for row in ledger] == ["registered", "failed"]
    assert ledger[0]["declared_trials"] == 72


def test_source_change_requires_a_new_counted_registration(tmp_path, monkeypatch):
    study = register_study(tmp_path/"family", write_data(tmp_path), development_end="2023-05-01T00:00:00Z",
                           protected_holdout_start="2023-05-03T00:00:00Z")
    monkeypatch.setattr("quantbot.factors.study.source_tree_hash", lambda: "different_source")
    with pytest.raises(ValueError, match="source changed"):
        run_study(study)
    assert (study/"failure.json").exists()


def test_long_windows_preserve_every_factor_admission_threshold():
    extended = study_protocol(validation_days=730, prior_hypotheses=51)
    for key, value in PROTOCOL.items():
        if key not in {"validation_days", "version", "multiplicity"}:
            assert extended[key] == value
    assert extended["validation_days"] == 730
    assert extended["correction_family_size"] == 102
    assert holm_adjust([0.001] + [1.0]*101)[0] == pytest.approx(0.102)


@pytest.mark.parametrize("days", [179, 0, -730, 730.5, True])
def test_validation_window_cannot_be_shortened_or_ambiguous(days):
    with pytest.raises(ValueError, match="at least 180"):
        study_protocol(days)


def test_evaluation_uses_the_entire_frozen_long_window_geometry():
    dates = pd.date_range("2018-01-01", periods=2700, tz="UTC")
    # Missing-factor fixture isolates window placement without inventing performance evidence.
    panel = pd.DataFrame([
        {"as_of": stamp, "symbol": symbol, "factor": definition.name,
         "value": np.nan, "missing_reason": "insufficient_history"}
        for definition in FACTORS for symbol in ("BTC/USDT", "ETH/USDT") for stamp in dates
    ])
    labels = pd.DataFrame([
        {"as_of": stamp, "symbol": symbol, "horizon": horizon, "exit_at": stamp+horizon*DAY,
         "net_return": 0.0, "gross_return": 0.0, "stress_return": -0.003,
         "relative_gross_return": 0.0, "missing_reason": None}
        for symbol in ("BTC/USDT", "ETH/USDT") for horizon in (1, 5, 10) for stamp in dates
    ])
    result = evaluate_panel(panel, labels, pd.DataFrame({"as_of": dates}),
                            trial_ids=[str(i) for i in range(72)], validation_days=730,
                            prior_p_values=[1.0]*51)
    assert result["validation_days"] == 730 and result["correction_family_size"] == 102
    assert result["pool"] == []
    folds = result["results"][0]["folds"]
    assert pd.Timestamp(folds[0]["start"]) == dates[-2190]
    assert pd.Timestamp(folds[-1]["end_exclusive"]) == dates[-1]+DAY
    assert all(pd.Timestamp(f["end_exclusive"])-pd.Timestamp(f["start"]) == 730*DAY for f in folds)


def test_prior_completed_factor_trials_are_imported_once_and_frozen(tmp_path):
    data = write_data(tmp_path)
    args = {"development_end": "2023-05-01T00:00:00Z", "protected_holdout_start": "2023-05-03T00:00:00Z"}
    first = register_study(tmp_path/"first-family", data, **args)
    run_study(first)
    second = register_study(tmp_path/"second-family", data, **args, validation_days=730,
                            prior_studies=(first, first))
    snapshot = json.loads((second/"prior-factor-evidence.json").read_text())
    assert len(snapshot) == 1 and snapshot[0]["status"] == "completed"
    assert len(snapshot[0]["inferential_trials"]) == 51
    second_result = run_study(second)
    assert second_result["correction_family_size"] == 102
    assert second_result["family_registered_trials"] == 144
    assert second_result["family_registered_studies"] == 2
    # The local second study already names first; explicit import must not count first twice.
    third = register_study(tmp_path/"second-family", data, **args, prior_studies=(first,))
    frozen = json.loads((third/"preregistration.json").read_text())
    assert frozen["protocol"]["prior_inferential_hypotheses"] == 102
    assert frozen["protocol"]["correction_family_size"] == 153


def test_failed_prior_trials_remain_in_the_next_family_correction(tmp_path):
    data = write_data(tmp_path)
    args = {"development_end": "2023-05-01T00:00:00Z", "protected_holdout_start": "2023-05-03T00:00:00Z"}
    failed = register_study(tmp_path/"family", data, **args)
    # The frozen registration is still intact; altered input makes this a failed attempt.
    with (failed/"data.csv").open("a") as handle:
        handle.write("\n")
    with pytest.raises(ValueError, match="frozen input changed"):
        run_study(failed)
    next_study = register_study(tmp_path/"family", data, **args)
    prior = json.loads((next_study/"prior-factor-evidence.json").read_text())
    assert prior[0]["status"] == "failed"
    assert len(prior[0]["inferential_trials"]) == 51
    assert all(trial["p_value"] == 1.0 for trial in prior[0]["inferential_trials"])
    assert run_study(next_study)["family_registered_trials"] == 144


def test_changed_prior_results_or_frozen_prior_snapshot_cannot_be_used(tmp_path):
    data = write_data(tmp_path)
    args = {"development_end": "2023-05-01T00:00:00Z", "protected_holdout_start": "2023-05-03T00:00:00Z"}
    first = register_study(tmp_path/"first", data, **args)
    run_study(first)
    second = register_study(tmp_path/"second", data, **args, prior_studies=(first,))
    (second/"prior-factor-evidence.json").write_text("[]\n")
    with pytest.raises(ValueError, match="prior-factor-evidence"):
        run_study(second)
    with (first/"result.json").open("a") as handle:
        handle.write("\n")
    with pytest.raises(ValueError, match="prior completed evidence changed"):
        register_study(tmp_path/"third", data, **args, prior_studies=(first,))

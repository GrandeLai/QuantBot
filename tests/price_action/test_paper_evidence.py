"""Paper 跨日证据门禁的纯函数测试。"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from quantbot.price_action.data.session import MarketSession
from quantbot.price_action.review.paper import (
    CAMPAIGN_HASH_MISMATCH,
    CLOSE_TIME_INVALID,
    FILL_MISMATCH,
    FILL_MISSING,
    LEGACY_EVIDENCE_INCOMPLETE,
    MISSING_TRADING_DAY,
    MARKET_SESSION_INVALID,
    ORDER_CHAIN_INCOMPLETE,
    RECONCILIATION_DRIFT,
    RECONCILIATION_UNKNOWN,
    RUN_FAILED,
    SLIPPAGE_MISSING,
    SIGNAL_DATE_INVALID,
    UNRESOLVED_ORDER,
    PaperCampaignSpec,
    PaperDayAssessment,
    PaperEvidenceSummary,
    assess_paper_session,
    audit_paper_campaign,
)


CONFIG_HASH = "a" * 64
SOURCE_HASH = "b" * 64
ACCOUNT_HASH = "c" * 64


def _dates(count: int) -> tuple[str, ...]:
    start = date(2026, 1, 2)
    return tuple((start + timedelta(days=offset)).isoformat() for offset in range(count))


def _campaign(
    trading_dates: tuple[str, ...], *, minimum: int = 60
) -> PaperCampaignSpec:
    first_signal = (date.fromisoformat(trading_dates[0]) - timedelta(days=1)).isoformat()
    return PaperCampaignSpec(
        campaign_id="paper-pa-v1",
        candidate_config_hash=CONFIG_HASH,
        source_hash=SOURCE_HASH,
        account_fingerprint=ACCOUNT_HASH,
        expected_trading_dates=trading_dates,
        expected_signal_dates=(first_signal, *trading_dates[:-1]),
        minimum_trading_days=minimum,
    )


def _run_metadata(
    day: str, attempt_id: str, campaign: PaperCampaignSpec, **extra
) -> str:
    return json.dumps(
        {
            "attempt_id": attempt_id,
            "campaign_id": "paper-pa-v1",
            "campaign_sha256": campaign.sha256,
            "execution_date": day,
            "source_hash": SOURCE_HASH,
            "account_fingerprint": ACCOUNT_HASH,
            "market_session": _session(day, campaign).to_dict(),
            **extra,
        },
        sort_keys=True,
    )


def _session(day: str, campaign: PaperCampaignSpec) -> MarketSession:
    signal = campaign.expected_signal_dates[campaign.expected_trading_dates.index(day)]
    return MarketSession(day, signal, f"{day}T09:30:00-05:00",
                         f"{day}T16:00:00-05:00", "alpaca:calendar")


def _valid_records(
    day: str,
    *,
    campaign: PaperCampaignSpec,
    terminal_status: str | None = None,
    include_fill: bool = True,
    fill_quantity: int = 10,
    reconciliation_status: str = "MATCH",
) -> dict[str, list[dict]]:
    attempt_id = f"attempt-{day}"
    start_id = f"run-start-{day}"
    records: dict[str, list[dict]] = {
        "runs": [
            {
                "id": start_id,
                "mode": "paper",
                "status": "started",
                "occurred_at": f"{day}T09:35:00-05:00",
                "recorded_at": f"{day}T09:35:01-05:00",
                "signal_date": (
                    date.fromisoformat(day) - timedelta(days=1)
                ).isoformat(),
                "config_hash": CONFIG_HASH,
                "data_status": "fresh",
                "metadata_json": _run_metadata(day, attempt_id, campaign),
            },
            {
                "id": f"run-completed-{day}",
                "mode": "paper",
                "status": "completed",
                "signal_date": (
                    date.fromisoformat(day) - timedelta(days=1)
                ).isoformat(),
                "config_hash": CONFIG_HASH,
                "data_status": "fresh",
                "metadata_json": _run_metadata(day, attempt_id, campaign),
            },
        ],
        "decisions": [],
        "orders": [],
        "fills": [],
        "position_events": [],
        "equity_snapshots": [
            {
                "id": f"equity-{day}",
                "run_id": start_id,
                "equity": 100_000.0,
                "cash": 50_000.0,
                "buying_power": 50_000.0,
                "position_count": 2,
                "occurred_at": f"{day}T16:05:00-05:00",
                "recorded_at": f"{day}T16:05:01-05:00",
                "metadata_json": json.dumps(
                    {
                        "phase": "close",
                        "attempt_id": attempt_id,
                        "campaign_id": "paper-pa-v1",
                        "campaign_sha256": campaign.sha256,
                        "account_fingerprint": ACCOUNT_HASH,
                        "market_session": _session(day, campaign).to_dict(),
                    }
                ),
            }
        ],
        "reconciliations": [
            {
                "id": f"reconciliation-{category}-{day}",
                "run_id": start_id,
                "category": category,
                "status": reconciliation_status,
                "occurred_at": f"{day}T16:05:00-05:00",
                "recorded_at": f"{day}T16:05:01-05:00",
                "details_json": json.dumps(
                    {
                        "target_attempt_id": attempt_id,
                        "campaign_sha256": campaign.sha256,
                        "account_fingerprint": ACCOUNT_HASH,
                        "phase": "close",
                        "market_session": _session(day, campaign).to_dict(),
                    }
                ),
            }
            for category in ("positions", "working_orders", "account")
        ],
    }
    if terminal_status is not None:
        client_id = f"client-{day}"
        common = {
            "run_id": start_id,
            "client_order_id": client_id,
            "symbol": "TST",
            "side": "buy",
            "quantity": 10,
            "reference_price": 100.0,
        }
        records["orders"] = [
            {"id": f"planned-{day}", "status": "planned", **common},
            {"id": f"submitted-{day}", "status": "submitted", **common},
            {"id": f"terminal-{day}", "status": terminal_status, **common},
        ]
        if terminal_status == "filled":
            records["orders"].append(
                {
                    "id": f"state-committed-{day}",
                    "status": "state_committed",
                    "metadata_json": json.dumps(
                        {"terminal_order_id": f"terminal-{day}"}
                    ),
                    **common,
                }
            )
        if include_fill:
            records["fills"] = [
                {
                    "id": f"fill-{day}",
                    "run_id": start_id,
                    "order_id": f"terminal-{day}",
                    "symbol": "TST",
                    "side": "buy",
                    "quantity": fill_quantity,
                    "price": 100.05,
                    "reference_price": 100.0,
                    "slippage_bps": 5.0,
                }
            ]
    return records


def test_contracts_round_trip_and_keep_stable_canonical_hashes():
    day = "2026-01-02"
    campaign = _campaign((day,), minimum=1)
    summary = audit_paper_campaign(
        campaign, {day: _valid_records(day, campaign=campaign)}
    )

    restored_campaign = PaperCampaignSpec.from_json(campaign.to_json())
    restored_day = PaperDayAssessment.from_json(
        summary.day_assessments[0].to_json()
    )
    restored_summary = PaperEvidenceSummary.from_json(summary.to_json())

    assert restored_campaign == campaign
    assert restored_day == summary.day_assessments[0]
    assert restored_summary == summary
    assert restored_campaign.sha256 == campaign.sha256
    assert restored_summary.sha256 == summary.sha256
    assert restored_summary.evidence_sha256 == summary.evidence_sha256


@pytest.mark.parametrize(
    ("table", "field", "value"),
    [("equity_snapshots", "occurred_at", "2026-01-02T09:35:00-05:00"),
     ("equity_snapshots", "recorded_at", "2026-01-02T09:35:00-05:00"),
     ("equity_snapshots", "recorded_at", "2026-01-03T16:05:00-05:00"),
     ("reconciliations", "occurred_at", "2026-01-02T09:35:00-05:00"),
     ("reconciliations", "recorded_at", "2026-01-03T16:05:00-05:00"),
     ("reconciliations", "occurred_at", "2026-01-02T16:05:00")],
)
def test_paper_close_requires_actual_observation_and_recording_after_same_day_close(table, field, value):
    day = "2026-01-02"
    campaign = _campaign((day,), minimum=1)
    records = _valid_records(day, campaign=campaign)
    records[table][0][field] = value
    assessment = assess_paper_session(campaign, day, records)
    assert not assessment.eligible
    assert CLOSE_TIME_INVALID in assessment.reason_codes


def test_paper_start_requires_frozen_authoritative_session():
    day = "2026-01-02"
    campaign = _campaign((day,), minimum=1)
    records = _valid_records(day, campaign=campaign)
    for row in records["runs"]:
        metadata = json.loads(row["metadata_json"])
        metadata.pop("market_session")
        row["metadata_json"] = json.dumps(metadata)
    assert MARKET_SESSION_INVALID in assess_paper_session(campaign, day, records).reason_codes


def test_standalone_shadow_reconcile_cannot_replace_or_poison_paper_close():
    day = "2026-01-02"
    campaign = _campaign((day,), minimum=1)
    records = _valid_records(day, campaign=campaign)
    records["runs"].append({"id": "shadow", "mode": "reconcile", "status": "unknown"})
    records["reconciliations"].append({"id": "shadow-result", "run_id": "shadow",
                                       "status": "UNKNOWN", "category": "positions",
                                       "details_json": "{}"})
    assert assess_paper_session(campaign, day, records).eligible
    records["reconciliations"] = records["reconciliations"][-1:]
    assert not assess_paper_session(campaign, day, records).eligible


@pytest.mark.parametrize(
    ("count", "expected_passed"),
    [(59, False), (60, True)],
)
def test_gate_requires_sixty_consecutive_eligible_trading_days(
    count: int, expected_passed: bool
):
    trading_dates = _dates(count)
    campaign = _campaign(trading_dates)
    records = {
        day: _valid_records(day, campaign=campaign) for day in trading_dates
    }

    summary = audit_paper_campaign(campaign, records)

    assert summary.passed is expected_passed
    assert summary.eligible_trading_days == count
    assert summary.consecutive_eligible_trading_days == count


def test_missing_day_resets_consecutive_suffix_instead_of_cherry_picking():
    trading_dates = _dates(60)
    missing = trading_dates[-5]
    campaign = _campaign(trading_dates)
    records = {
        day: _valid_records(day, campaign=campaign)
        for day in trading_dates
        if day != missing
    }

    summary = audit_paper_campaign(campaign, records)

    assert not summary.passed
    assert summary.eligible_trading_days == 59
    assert summary.consecutive_eligible_trading_days == 4
    assert summary.missing_dates == (missing,)
    assert summary.day_assessments[-5].reason_codes == (MISSING_TRADING_DAY,)


@pytest.mark.parametrize(
    "terminal_status",
    ["planned", "submitted", "uncertain", "submit_error", "dead"],
)
def test_unresolved_order_statuses_fail_closed(terminal_status: str):
    day = "2026-01-02"
    campaign = _campaign((day,), minimum=1)
    records = _valid_records(day, campaign=campaign)
    start_id = records["runs"][0]["id"]
    common = {
        "run_id": start_id,
        "client_order_id": "client-unresolved",
        "symbol": "TST",
        "side": "buy",
        "quantity": 10,
        "reference_price": 100.0,
    }
    if terminal_status == "planned":
        records["orders"] = [{"id": "planned", "status": "planned", **common}]
    elif terminal_status == "submitted":
        records["orders"] = [
            {"id": "planned", "status": "planned", **common},
            {"id": "submitted", "status": "submitted", **common},
        ]
    else:
        records["orders"] = [
            {"id": "planned", "status": "planned", **common},
            {"id": "submitted", "status": "submitted", **common},
            {"id": "terminal", "status": terminal_status, **common},
        ]

    assessment = assess_paper_session(campaign, day, records)

    assert not assessment.eligible
    assert UNRESOLVED_ORDER in assessment.reason_codes


@pytest.mark.parametrize(
    ("terminal_status", "include_fill", "fill_quantity", "reason"),
    [
        ("filled", False, 0, FILL_MISSING),
        ("filled", True, 9, FILL_MISMATCH),
        ("partial", True, 10, FILL_MISMATCH),
    ],
)
def test_filled_and_partial_orders_require_matching_fill_evidence(
    terminal_status: str,
    include_fill: bool,
    fill_quantity: int,
    reason: str,
):
    day = "2026-01-02"
    campaign = _campaign((day,), minimum=1)
    records = _valid_records(
        day,
        campaign=campaign,
        terminal_status=terminal_status,
        include_fill=include_fill,
        fill_quantity=fill_quantity,
    )

    assessment = assess_paper_session(campaign, day, records)

    assert not assessment.eligible
    assert reason in assessment.reason_codes


def test_filled_order_with_complete_slippage_evidence_is_eligible():
    day = "2026-01-02"
    campaign = _campaign((day,), minimum=1)

    assessment = assess_paper_session(
        campaign,
        day,
        _valid_records(day, campaign=campaign, terminal_status="filled"),
    )

    assert assessment.eligible
    assert assessment.order_count == 1
    assert assessment.fill_count == 1
    assert assessment.slippage_sample_count == 1


def test_fill_without_reference_price_or_slippage_fails_closed():
    day = "2026-01-02"
    campaign = _campaign((day,), minimum=1)
    records = _valid_records(day, campaign=campaign, terminal_status="filled")
    records["fills"][0]["slippage_bps"] = None

    assessment = assess_paper_session(campaign, day, records)

    assert not assessment.eligible
    assert SLIPPAGE_MISSING in assessment.reason_codes


def test_filled_order_requires_state_commit_after_terminal_event():
    day = "2026-01-02"
    campaign = _campaign((day,), minimum=1)
    records = _valid_records(day, campaign=campaign, terminal_status="filled")
    records["orders"] = [
        row for row in records["orders"] if row["status"] != "state_committed"
    ]

    assessment = assess_paper_session(campaign, day, records)

    assert not assessment.eligible
    assert ORDER_CHAIN_INCOMPLETE in assessment.reason_codes


def test_explicit_rejection_without_fill_is_a_closed_order_lifecycle():
    day = "2026-01-02"
    campaign = _campaign((day,), minimum=1)

    assessment = assess_paper_session(
        campaign,
        day,
        _valid_records(
            day,
            campaign=campaign,
            terminal_status="rejected",
            include_fill=False,
        ),
    )

    assert assessment.eligible


def test_legacy_v1_facts_never_count_as_paper_evidence():
    day = "2026-01-02"
    campaign = _campaign((day,), minimum=1)
    records = _valid_records(day, campaign=campaign)
    records["runs"] = [
        {
            "id": "legacy-run",
            "mode": "paper",
            "status": "completed",
            "config_hash": CONFIG_HASH,
            "data_status": "fresh",
            "metadata_json": json.dumps({"market_level": 2}),
        }
    ]

    assessment = assess_paper_session(campaign, day, records)

    assert not assessment.eligible
    assert assessment.reason_codes == (LEGACY_EVIDENCE_INCOMPLETE,)


def test_completed_and_failed_on_same_day_is_not_eligible():
    day = "2026-01-02"
    campaign = _campaign((day,), minimum=1)
    records = _valid_records(day, campaign=campaign)
    records["runs"].append(
        {
            "id": "run-failed",
            "mode": "paper",
            "status": "failed",
            "config_hash": CONFIG_HASH,
            "data_status": "fresh",
            "metadata_json": _run_metadata(day, f"attempt-{day}", campaign),
        }
    )

    assessment = assess_paper_session(campaign, day, records)

    assert not assessment.eligible
    assert RUN_FAILED in assessment.reason_codes


@pytest.mark.parametrize(
    ("status", "reason"),
    [("UNKNOWN", RECONCILIATION_UNKNOWN), ("DRIFT", RECONCILIATION_DRIFT)],
)
def test_every_required_reconciliation_category_must_match(status: str, reason: str):
    day = "2026-01-02"
    campaign = _campaign((day,), minimum=1)
    records = _valid_records(
        day, campaign=campaign, reconciliation_status=status
    )

    assessment = assess_paper_session(campaign, day, records)

    assert not assessment.eligible
    assert reason in assessment.reason_codes


def test_runtime_facts_are_bound_to_complete_campaign_hash():
    day = "2026-01-02"
    original = _campaign((day,), minimum=1)
    changed = _campaign((day,), minimum=2)
    records = _valid_records(day, campaign=original)

    assessment = assess_paper_session(changed, day, records)

    assert not assessment.eligible
    assert CAMPAIGN_HASH_MISMATCH in assessment.reason_codes


def test_required_reconciliation_categories_cannot_be_reduced():
    day = "2026-01-02"
    with pytest.raises(ValueError, match="不得移除"):
        PaperCampaignSpec(
            campaign_id="paper-pa-v1",
            candidate_config_hash=CONFIG_HASH,
            source_hash=SOURCE_HASH,
            account_fingerprint=ACCOUNT_HASH,
            expected_trading_dates=(day,),
            expected_signal_dates=("2026-01-01",),
            minimum_trading_days=1,
            required_reconciliation_categories=("positions",),
        )


def test_signal_date_must_equal_frozen_previous_session():
    day = "2026-01-02"
    campaign = _campaign((day,), minimum=1)
    records = _valid_records(day, campaign=campaign)
    for row in records["runs"]:
        row["signal_date"] = "2025-12-31"

    assessment = assess_paper_session(campaign, day, records)

    assert not assessment.eligible
    assert SIGNAL_DATE_INVALID in assessment.reason_codes

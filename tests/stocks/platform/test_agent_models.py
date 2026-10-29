"""Tests for structured assistant advice contracts."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from quantbot.common.contracts.agent_advice import AdviceCard, AdviceEvidence


def test_advice_card_requires_evidence_and_risk_notes() -> None:
    card = AdviceCard(
        type="opportunity",
        subject="AAPL",
        recommendation="watch",
        confidence=0.72,
        evidence=[
            AdviceEvidence(
                source="screener",
                summary="relative strength improving",
                observed_at=datetime(2026, 4, 13, tzinfo=UTC),
            )
        ],
        risk_notes=["earnings this week"],
        generated_at=datetime(2026, 4, 13, tzinfo=UTC),
        freshness="fresh",
    )

    assert card.type == "opportunity"
    assert card.evidence[0].source == "screener"
    assert card.risk_notes == ["earnings this week"]


@pytest.mark.parametrize(
    ("evidence", "risk_notes", "expected_field"),
    [
        ([], ["earnings this week"], "evidence"),
        (
            [
                AdviceEvidence(
                    source="screener",
                    summary="relative strength improving",
                    observed_at=datetime(2026, 4, 13, tzinfo=UTC),
                )
            ],
            [],
            "risk_notes",
        ),
    ],
)
def test_advice_card_rejects_empty_evidence_or_risk_notes(
    evidence: list[AdviceEvidence],
    risk_notes: list[str],
    expected_field: str,
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        AdviceCard(
            type="opportunity",
            subject="AAPL",
            recommendation="watch",
            confidence=0.72,
            evidence=evidence,
            risk_notes=risk_notes,
            generated_at=datetime(2026, 4, 13, tzinfo=UTC),
            freshness="fresh",
        )

    assert expected_field in str(exc_info.value)

"""Tests for shared platform contract models."""

from datetime import UTC, datetime

from pydantic import ValidationError

from quantbot.common.platform.models import DomainStatus, FactEnvelope


def test_domain_status_exposes_required_fields() -> None:
    status = DomainStatus(
        name="market_data",
        status="ok",
        freshness="fresh",
        updated_at=datetime(2026, 4, 13, tzinfo=UTC),
        detail="duckdb reachable",
    )

    assert status.name == "market_data"
    assert status.status == "ok"
    assert status.freshness == "fresh"


def test_fact_envelope_requires_version_and_payload() -> None:
    envelope = FactEnvelope(
        version="2026-04-13",
        generated_at=datetime(2026, 4, 13, tzinfo=UTC),
        payload={"positions": 2},
    )

    assert envelope.version == "2026-04-13"
    assert envelope.payload["positions"] == 2


def test_domain_status_rejects_unknown_status() -> None:
    try:
        DomainStatus(
            name="risk",
            status="green",
            freshness="fresh",
            updated_at=datetime(2026, 4, 13, tzinfo=UTC),
        )
    except ValidationError as exc:
        assert "status" in str(exc)
    else:
        raise AssertionError("expected ValidationError for unsupported status")

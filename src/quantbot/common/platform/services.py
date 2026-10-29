"""Read-only platform service facade."""

from __future__ import annotations

from datetime import UTC, datetime

from quantbot.common.platform.models import DomainStatus, FactEnvelope


def _build_placeholder_domain_status(name: str, updated_at: datetime) -> dict[str, str | datetime]:
    """Return an explicit placeholder until a real probe is wired."""
    return DomainStatus(
        name=name,
        status="degraded",
        freshness="unknown",
        updated_at=updated_at,
        detail="Domain probe is not wired yet.",
    ).model_dump()


def build_platform_summary() -> FactEnvelope:
    """Return a shared-platform domain health snapshot."""
    now = datetime.now(UTC)
    domains = {
        "market_data": _build_placeholder_domain_status("market_data", now),
        "portfolio_account": _build_placeholder_domain_status("portfolio_account", now),
        "strategy_validation": _build_placeholder_domain_status("strategy_validation", now),
        "risk": _build_placeholder_domain_status("risk", now),
        "execution": _build_placeholder_domain_status("execution", now),
        "agent": _build_placeholder_domain_status("agent", now),
    }
    return FactEnvelope(
        version=now.date().isoformat(),
        generated_at=now,
        payload={"domains": domains},
    )

"""Shared platform API regression tests."""

from fastapi.testclient import TestClient

from quantbot.common.platform.models import PlatformSummaryResponse


def test_platform_summary_lists_all_domains(client: TestClient) -> None:
    """Platform summary should expose all shared product domains."""
    response = client.get("/api/platform/summary")
    assert response.status_code == 200

    body = response.json()
    summary = PlatformSummaryResponse.model_validate(body)
    assert body["version"]
    assert set(body["domains"].keys()) == {
        "market_data",
        "portfolio_account",
        "strategy_validation",
        "risk",
        "execution",
        "agent",
    }
    assert summary.domains["market_data"].status == "degraded"
    assert summary.domains["market_data"].freshness == "unknown"
    assert summary.domains["market_data"].detail == "Domain probe is not wired yet."

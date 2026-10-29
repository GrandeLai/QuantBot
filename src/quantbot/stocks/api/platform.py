"""Shared platform API endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from quantbot.common.platform.models import PlatformSummaryResponse
from quantbot.common.platform.services import build_platform_summary

router = APIRouter(prefix="/platform", tags=["platform"])


@router.get("/summary", response_model=PlatformSummaryResponse)
async def get_platform_summary() -> PlatformSummaryResponse:
    """Return read-only shared platform status."""
    envelope = build_platform_summary()
    return PlatformSummaryResponse(
        version=envelope.version,
        generated_at=envelope.generated_at,
        domains=envelope.payload["domains"],
    )

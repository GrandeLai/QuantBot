"""Structured advice card contracts shared by workbench and assistant frontends."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from quantbot.common.platform.models import FreshnessState

AdviceType = Literal[
    "opportunity",
    "rebalance_recommendation",
    "risk_alert",
    "strategy_diagnosis",
    "portfolio_review",
]


class AdviceEvidence(BaseModel):
    """Evidence attached to a structured advice card."""

    source: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    observed_at: datetime


class AdviceCard(BaseModel):
    """Shared AI suggestion payload."""

    type: AdviceType
    subject: str = Field(min_length=1)
    recommendation: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: Annotated[list[AdviceEvidence], Field(min_length=1)]
    risk_notes: Annotated[list[str], Field(min_length=1)]
    generated_at: datetime
    freshness: FreshnessState

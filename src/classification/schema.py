"""Pydantic schema for LLM classification output, used to validate the strict
JSON the model is instructed to return."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

EventType = Literal[
    "earnings_surprise",
    "guidance_change",
    "ma_deal",
    "analyst_rating",
    "regulatory_legal",
    "insider_activity",
    "partnership_contract",
    "macro_sector",
    "other",
]

Direction = Literal["bullish", "bearish", "neutral"]
ImpactHorizon = Literal["intraday", "1_3_days", "swing", "long_term", "unknown"]


class ClassificationResult(BaseModel):
    event_type: EventType
    direction: Direction
    reason: str
    magnitude_pct: Optional[float] = None
    materiality_score: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="How likely this item is to be genuinely stock-moving.",
    )
    impact_horizon: ImpactHorizon = "unknown"

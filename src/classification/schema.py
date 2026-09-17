"""Pydantic schema for LLM classification output, used to validate the strict
JSON the model is instructed to return."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# The four types after ma_deal were added 2026-09-17. Until then the taxonomy
# had no home for a negative corporate catalyst, so the model filed them under
# whatever was nearest and the alert layer never saw them:
#   credit rating        -> analyst_rating (43 of 46 delivered analyst_rating
#                           alerts were rating-agency filings, not broker calls)
#   insolvency / NCLT    -> regulatory_legal, 22 of 43 as NEUTRAL
#   resignation / demise -> other, 5 of 10 as NEUTRAL
# A neutral direction never alerts (src/scoring/source_quality.py), so a CEO's
# death produced a neutral "other" and went out to nobody.
#
# Deliberately NOT split out: litigation stays inside regulatory_legal (which
# measures 75% at 3d — no evidence it is broken), and plant fire/shutdown stays
# in other (too few filings to justify a bucket). Splitting costs sample size,
# so it needs a reason each time.
EventType = Literal[
    "earnings_surprise",
    "guidance_change",
    "ma_deal",
    "credit_rating",
    "management_change",
    "insolvency",
    "default_payment",
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
    # A clean, factual one-line summary of what the filing actually says (like the
    # pro platforms' headlines), e.g. "Reports FY26 net profit up 29% to Rs 236 cr".
    # Defaults empty for older/mocked results and media items.
    headline: str = ""
    magnitude_pct: Optional[float] = None
    materiality_score: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="How likely this item is to be genuinely stock-moving.",
    )
    impact_horizon: ImpactHorizon = "unknown"

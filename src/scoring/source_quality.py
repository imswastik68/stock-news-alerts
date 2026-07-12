"""Source credibility scoring and alert gate helpers.

The numbers are deliberately conservative priors, not truth. They let the
pipeline favor official filings over broad news feeds when deciding whether a
classified article is worthy of a directional alert.
"""

from __future__ import annotations

from src.classification.schema import ClassificationResult

SOURCE_QUALITY = {
    "nse_announcements": 1.0,
    "bse_announcements": 1.0,
    "moneycontrol": 0.75,
    "economic_times": 0.75,
    "newsapi": 0.70,
    "google_news": 0.55,
}


def get_source_quality(source: str) -> float:
    return SOURCE_QUALITY.get(source, 0.40)


def is_directional_material_alert(
    result: ClassificationResult,
    *,
    confidence: float,
    source_quality: float,
    alert_confidence_threshold: float,
    min_materiality_score: float,
    min_source_quality_for_alerts: float,
    excluded_event_types: set[str],
    impact_tier: str = "",
) -> bool:
    if result.event_type in excluded_event_types:
        return False
    if result.direction == "neutral":
        return False
    if source_quality < min_source_quality_for_alerts:
        return False
    # A HIGH-impact exchange category (order win, M&A, results, rating, penalty,
    # buyback, insolvency…) with a directional LLM view is a genuine catalyst —
    # alert it regardless of the event-type confidence prior OR the materiality
    # estimate. This bypass is essential: most event types' base rates sit below
    # the 0.70 threshold (order wins/penalties = 0.55/0.60), so without it a real
    # order win or SEBI penalty could NEVER alert. Runs before the confidence
    # check for exactly that reason.
    if impact_tier == "high":
        return True
    if confidence < alert_confidence_threshold:
        return False
    return result.materiality_score >= min_materiality_score

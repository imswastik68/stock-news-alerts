"""Source credibility scoring and alert gate helpers.

The numbers are deliberately conservative priors, not truth. They let the
pipeline favor official filings over broad news feeds when deciding whether a
classified article is worthy of a directional alert.
"""

from __future__ import annotations

from src.classification.schema import ClassificationResult

SOURCE_QUALITY = {
    "nse_announcements": 1.0,
    "bse_rss": 1.0,
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
    high_tier_confidence_threshold: float = 0.0,
    directionally_unreliable_event_types: frozenset[str] | set[str] = frozenset(),
) -> bool:
    if result.event_type == "classification_failed":
        return False
    if result.direction == "neutral":
        return False
    if source_quality < min_source_quality_for_alerts:
        return False
    # Event types with a measured directional record bad enough that we don't
    # claim a direction at all. Checked BEFORE the HIGH-tier path because the
    # problem isn't whether the news matters (it does) — it's that our call on
    # which way it moves the stock has no demonstrated edge. ma_deal was
    # 0-for-35 in production while carrying the table's highest prior (0.90).
    if result.event_type in directionally_unreliable_event_types:
        return False
    # A HIGH-impact exchange category (order win, M&A, results, rating, penalty,
    # bonus, dividend, buyback…) with a directional view is a genuine catalyst,
    # so it gets a much lower confidence bar than a media item — but NOT an
    # unconditional pass. It used to `return True` here, which meant confidence
    # was never consulted for 94% of alerts and the entire calibration loop had
    # no effect on what got sent; an event type could measure 0% forever and
    # keep alerting. The floor is what gives calibration teeth: as measured
    # hit-rates decay, the shrunk confidence eventually drops below it and that
    # event type goes quiet on its own. Still deliberately ahead of the "other"
    # exclusion, so a bonus/split/dividend bucketed as "other" can alert.
    if impact_tier == "high":
        return confidence >= high_tier_confidence_threshold
    if result.event_type in excluded_event_types:
        return False
    # Any other exchange filing (impact_tier set = it came from NSE/BSE): the LLM
    # read the actual PDF content, so trust its materiality estimate directly and
    # ignore the weak event-type confidence prior (most priors sit below 0.70, so
    # a real order win hidden in a "General Updates" filing could never alert
    # otherwise). This is the core of reading filings like the pro platforms do.
    if impact_tier:
        return result.materiality_score >= min_materiality_score
    # Media items (no exchange category, no PDF): keep the stricter gate — need
    # both the confidence prior and the materiality estimate.
    if confidence < alert_confidence_threshold:
        return False
    return result.materiality_score >= min_materiality_score

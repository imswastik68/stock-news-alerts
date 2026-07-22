from __future__ import annotations

from src.classification.schema import ClassificationResult
from src.scoring.source_quality import get_source_quality, is_directional_material_alert


def _result(**overrides):
    data = {
        "event_type": "earnings_surprise",
        "direction": "bullish",
        "reason": "EPS beat estimates.",
        "magnitude_pct": 12.0,
        "materiality_score": 0.8,
        "impact_horizon": "1_3_days",
    }
    data.update(overrides)
    return ClassificationResult(**data)


def test_official_sources_score_highest():
    assert get_source_quality("nse_announcements") == 1.0
    assert get_source_quality("unknown_blog") == 0.40


def test_alert_gate_requires_directional_material_high_quality_news():
    assert is_directional_material_alert(
        _result(),
        confidence=0.72,
        source_quality=0.70,
        alert_confidence_threshold=0.70,
        min_materiality_score=0.65,
        min_source_quality_for_alerts=0.55,
        excluded_event_types={"other", "classification_failed"},
    )


def test_high_impact_directional_alerts_even_below_confidence_threshold():
    # An order win (partnership_contract, base 0.55) can never reach the 0.70
    # confidence threshold — but a HIGH-impact category + directional view must
    # still alert. This is the fix for "most catalysts never alert".
    common = dict(
        confidence=0.58, source_quality=1.0, alert_confidence_threshold=0.70,
        min_materiality_score=0.65, min_source_quality_for_alerts=0.55,
        excluded_event_types={"other", "classification_failed"},
    )
    assert is_directional_material_alert(
        _result(event_type="partnership_contract", direction="bullish", materiality_score=0.5),
        impact_tier="high", **common,
    )
    # but a NEUTRAL high-impact filing still must not alert (no directional view)
    assert not is_directional_material_alert(
        _result(event_type="partnership_contract", direction="neutral"),
        impact_tier="high", **common,
    )


# ── HIGH tier is no longer an unconditional pass ─────────────────────────────
# Until 2026-07-22 `impact_tier == "high"` did `return True`, so confidence was
# never consulted for 94% of alerts and the calibration loop had no effect on
# what got sent — an event type could measure 0% forever and keep alerting.

_GATE = dict(
    source_quality=1.0, alert_confidence_threshold=0.70,
    min_materiality_score=0.65, min_source_quality_for_alerts=0.55,
    excluded_event_types={"other", "classification_failed"},
)


def test_high_tier_below_confidence_floor_is_blocked():
    # The regression that matters: a HIGH-tier filing whose calibrated
    # confidence has decayed under the floor must go quiet by itself.
    assert not is_directional_material_alert(
        _result(event_type="partnership_contract", materiality_score=0.9),
        confidence=0.20, impact_tier="high",
        high_tier_confidence_threshold=0.35, **_GATE,
    )


def test_high_tier_above_confidence_floor_still_alerts():
    # ...while a normal catalyst still clears the deliberately-low floor, so
    # this stays a safety net rather than a second strict threshold.
    assert is_directional_material_alert(
        _result(event_type="partnership_contract", materiality_score=0.5),
        confidence=0.40, impact_tier="high",
        high_tier_confidence_threshold=0.35, **_GATE,
    )


def test_directionally_unreliable_event_type_blocked_even_at_high_tier():
    # ma_deal measured 0-for-35 in production while carrying the table's
    # HIGHEST prior (0.90). High confidence and HIGH tier must not save it.
    assert not is_directional_material_alert(
        _result(event_type="ma_deal", materiality_score=0.95),
        confidence=0.95, impact_tier="high",
        high_tier_confidence_threshold=0.35,
        directionally_unreliable_event_types=frozenset({"ma_deal"}), **_GATE,
    )


def test_unreliable_list_does_not_affect_other_event_types():
    assert is_directional_material_alert(
        _result(event_type="partnership_contract", materiality_score=0.5),
        confidence=0.40, impact_tier="high",
        high_tier_confidence_threshold=0.35,
        directionally_unreliable_event_types=frozenset({"ma_deal"}), **_GATE,
    )


def test_alert_gate_rejects_neutral_low_materiality_or_low_quality():
    common = {
        "confidence": 0.90,
        "alert_confidence_threshold": 0.70,
        "min_materiality_score": 0.65,
        "min_source_quality_for_alerts": 0.55,
        "excluded_event_types": {"other", "classification_failed"},
    }
    assert not is_directional_material_alert(_result(direction="neutral"), source_quality=1.0, **common)
    assert not is_directional_material_alert(_result(materiality_score=0.30), source_quality=1.0, **common)
    assert not is_directional_material_alert(_result(), source_quality=0.40, **common)

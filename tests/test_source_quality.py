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
    assert get_source_quality("bse_announcements") == 1.0
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

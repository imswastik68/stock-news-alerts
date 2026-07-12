"""Tests for the static confidence scoring table and magnitude nudge — the
other fragile-but-critical piece (a scoring bug either spams low-quality
alerts or silently suppresses real ones)."""

from __future__ import annotations

from src.classification.schema import ClassificationResult
from src.scoring.confidence import StaticTableConfidenceProvider

BASE_RATES = {
    "ma_deal": 0.90,
    "earnings_surprise": 0.68,
    "guidance_change": 0.65,
    "regulatory_legal": 0.60,
    "analyst_rating": 0.55,
    "insider_activity": 0.55,
    "partnership_contract": 0.55,
    "macro_sector": 0.50,
    "other": 0.40,
}


def _result(event_type, direction="bullish", reason="x", magnitude_pct=None):
    return ClassificationResult(
        event_type=event_type, direction=direction, reason=reason, magnitude_pct=magnitude_pct
    )


def test_base_rate_returned_for_each_event_type_with_no_magnitude():
    provider = StaticTableConfidenceProvider(BASE_RATES)
    for event_type, expected in BASE_RATES.items():
        confidence = provider.get_confidence(_result(event_type))
        assert confidence == expected, f"{event_type}: expected {expected}, got {confidence}"


def test_magnitude_nudge_added_for_eligible_event_types():
    provider = StaticTableConfidenceProvider(BASE_RATES)
    # 4% magnitude -> +0.02 nudge (4 * 0.005)
    confidence = provider.get_confidence(_result("earnings_surprise", magnitude_pct=4.0))
    assert confidence == round(0.68 + 0.02, 4)


def test_magnitude_nudge_clamped_at_cap():
    provider = StaticTableConfidenceProvider(BASE_RATES)
    # 100% magnitude would be +0.5 uncapped; nudge caps at +0.05
    confidence = provider.get_confidence(_result("earnings_surprise", magnitude_pct=100.0))
    assert confidence == round(0.68 + 0.05, 4)


def test_magnitude_ignored_for_non_eligible_event_types():
    provider = StaticTableConfidenceProvider(BASE_RATES)
    confidence = provider.get_confidence(_result("ma_deal", magnitude_pct=50.0))
    assert confidence == 0.90


def test_magnitude_none_means_no_adjustment():
    provider = StaticTableConfidenceProvider(BASE_RATES)
    confidence = provider.get_confidence(_result("analyst_rating", magnitude_pct=None))
    assert confidence == 0.55


def test_high_materiality_adds_small_confidence_nudge():
    provider = StaticTableConfidenceProvider(BASE_RATES)
    result = _result("partnership_contract")
    result.materiality_score = 0.90
    confidence = provider.get_confidence(result)
    assert confidence == round(0.55 + ((0.90 - 0.5) * 0.08), 4)


def test_low_materiality_reduces_confidence():
    provider = StaticTableConfidenceProvider(BASE_RATES)
    result = _result("partnership_contract")
    result.materiality_score = 0.20
    confidence = provider.get_confidence(result)
    assert confidence == round(0.55 + ((0.20 - 0.5) * 0.08), 4)


def test_negative_magnitude_uses_absolute_value():
    provider = StaticTableConfidenceProvider(BASE_RATES)
    # a -6% miss is still a 6%-magnitude surprise -> nudge uses abs()
    confidence = provider.get_confidence(_result("guidance_change", magnitude_pct=-6.0))
    assert confidence == round(0.65 + 0.03, 4)


def test_final_confidence_clamped_at_max():
    high_base = {**BASE_RATES, "earnings_surprise": 0.97}
    provider = StaticTableConfidenceProvider(high_base)
    confidence = provider.get_confidence(_result("earnings_surprise", magnitude_pct=100.0))
    assert confidence == 0.99


def test_unknown_event_type_falls_back_to_other_rate():
    provider = StaticTableConfidenceProvider(BASE_RATES)
    assert provider._base_rate("some_future_event_type") == BASE_RATES["other"]

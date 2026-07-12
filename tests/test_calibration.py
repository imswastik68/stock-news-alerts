"""Tests for the calibrated confidence model — the piece that turns the hand-tuned
prior into a measured one via Bayesian shrinkage toward the empirical hit-rate."""

from __future__ import annotations

from unittest.mock import patch

from src.classification.schema import ClassificationResult
from src.scoring.confidence import BacktestedConfidenceProvider, _SHRINKAGE_K

BASE = {"earnings_surprise": 0.68, "partnership_contract": 0.55, "other": 0.40}


def _res(event_type, direction="bullish", mat=0.5):
    return ClassificationResult(
        event_type=event_type, direction=direction, reason="x", materiality_score=mat
    )


def _provider(stats):
    with patch("src.storage.db.get_hit_rate_stats", return_value=stats):
        return BacktestedConfidenceProvider(session=None, base_rates=BASE)


def test_no_outcomes_falls_back_to_prior():
    # materiality 0.5 => no nudge, so confidence == the static prior exactly.
    p = _provider({})
    assert p.get_confidence(_res("earnings_surprise")) == 0.68


def test_empirical_rate_shrinks_toward_prior():
    # order wins hit 90% over 30 matured samples; prior 0.55.
    p = _provider({"partnership_contract": {"n": 30, "hits": 27}})
    expected = (30 * 0.9 + _SHRINKAGE_K * 0.55) / (30 + _SHRINKAGE_K)
    assert abs(p.get_confidence(_res("partnership_contract")) - round(expected, 4)) < 0.005
    # and it moved meaningfully up from the 0.55 prior toward the 0.90 evidence
    assert p.get_confidence(_res("partnership_contract")) > 0.70


def test_small_sample_stays_near_prior():
    # a lucky 2/2 must NOT claim ~100% — shrinkage keeps it near the 0.55 prior.
    p = _provider({"partnership_contract": {"n": 2, "hits": 2}})
    conf = p.get_confidence(_res("partnership_contract"))
    assert conf < 0.65


def test_poor_track_record_lowers_confidence():
    # if a category historically misses, confidence drops below its prior.
    p = _provider({"earnings_surprise": {"n": 40, "hits": 12}})  # 30% hit-rate
    assert p.get_confidence(_res("earnings_surprise")) < 0.68

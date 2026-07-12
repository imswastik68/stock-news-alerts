"""
Confidence scoring for classified articles.

Two implementations behind the ConfidenceProvider interface:

  StaticTableConfidenceProvider     — the hand-tuned prior from confidence_table.yaml
                                       (subjective, not backtested).
  BacktestedConfidenceProvider      — blends that prior with the EMPIRICAL hit-rate
                                       measured from tracked alert outcomes
                                       (src/scoring/outcomes.py), via Bayesian
                                       shrinkage. Early on it's ~the prior; as real
                                       outcomes accumulate it becomes calibrated —
                                       an "82%" starts to actually mean 82%.

The pipeline uses the backtested provider; with no outcome history yet it falls
back cleanly to the static prior.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from sqlalchemy.orm import Session

from src.classification.schema import ClassificationResult
from src.config import get_settings

# Magnitude nudge only applies to event types where magnitude_pct is a
# proportional beat/miss or rating-change size (bigger number = more confident
# in the predicted direction). Other event types either don't have a
# meaningful magnitude (ma_deal) or the LLM was told not to invent one.
_MAGNITUDE_ELIGIBLE_EVENT_TYPES = {"earnings_surprise", "guidance_change", "analyst_rating"}

# adjustment = clamp(abs(magnitude_pct) * MAGNITUDE_SCALE, 0, MAGNITUDE_CAP)
# e.g. a 10% EPS beat -> +0.05 (the max); a 2% beat -> +0.01. Deliberately
# simple and linear, not a black box — see module docstring.
_MAGNITUDE_SCALE = 0.005
_MAGNITUDE_CAP = 0.05
_MATERIALITY_SCALE = 0.08

_MIN_CONFIDENCE = 0.0
_MAX_CONFIDENCE = 0.99


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class ConfidenceProvider(ABC):
    @abstractmethod
    def get_confidence(self, result: ClassificationResult) -> float:
        raise NotImplementedError


class StaticTableConfidenceProvider(ConfidenceProvider):
    """Base rate looked up from confidence_table.yaml by event_type, nudged by
    the magnitude of any extracted quantifiable detail. Unknown event types
    (shouldn't happen given the pydantic Literal, but defensive) fall back to
    the 'other' rate."""

    def __init__(self, base_rates: dict[str, float] | None = None):
        self._base_rates = base_rates if base_rates is not None else get_settings().confidence_base_rates

    def _base_rate(self, event_type: str) -> float:
        return self._base_rates.get(event_type, self._base_rates.get("other", 0.40))

    def _magnitude_adjustment(self, result: ClassificationResult) -> float:
        if result.event_type not in _MAGNITUDE_ELIGIBLE_EVENT_TYPES:
            return 0.0
        if result.magnitude_pct is None:
            return 0.0
        return _clamp(abs(result.magnitude_pct) * _MAGNITUDE_SCALE, 0.0, _MAGNITUDE_CAP)

    def _materiality_adjustment(self, result: ClassificationResult) -> float:
        # 0.5 is neutral, so older classifier test fixtures and uncertain LLM
        # outputs keep the table score unchanged. Clearly material stories get
        # a small boost; noisy/recycled stories get pushed away from alerts.
        return (result.materiality_score - 0.5) * _MATERIALITY_SCALE

    def get_confidence(self, result: ClassificationResult) -> float:
        base = self._base_rate(result.event_type)
        adjustment = self._magnitude_adjustment(result)
        adjustment += self._materiality_adjustment(result)
        return round(_clamp(base + adjustment, _MIN_CONFIDENCE, _MAX_CONFIDENCE), 4)


# Pseudo-count for Bayesian shrinkage: with K prior "observations", an event type
# needs a few dozen real outcomes before the empirical rate meaningfully overrides
# the prior. Prevents a lucky 3/3 start from claiming 100% confidence.
_SHRINKAGE_K = 15


class BacktestedConfidenceProvider(StaticTableConfidenceProvider):
    """Static prior shrunk toward the empirical hit-rate measured from tracked
    outcomes. base_rate(e) = (n·hit_rate + K·prior) / (n + K)."""

    def __init__(self, session: Session, base_rates: dict[str, float] | None = None):
        super().__init__(base_rates)
        from src.storage.db import get_hit_rate_stats

        try:
            self._stats = get_hit_rate_stats(session)
        except Exception:
            self._stats = {}

    def _base_rate(self, event_type: str) -> float:
        prior = super()._base_rate(event_type)
        s = self._stats.get(event_type)
        if not s or s["n"] <= 0:
            return prior
        hit_rate = s["hits"] / s["n"]
        return (s["n"] * hit_rate + _SHRINKAGE_K * prior) / (s["n"] + _SHRINKAGE_K)

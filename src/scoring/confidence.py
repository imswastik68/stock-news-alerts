"""
Confidence scoring for classified articles.

IMPORTANT: the static table below is a starting heuristic, not a statistically
validated model. It has not been backtested against actual price outcomes —
treat alert confidence as a filter for your own research, not a signal to act
on directly. See confidence_table.yaml for the base rates and rationale.

ConfidenceProvider is an interface so a future backtested implementation (e.g.
one that learns event_type -> forward-return-hit-rate from the articles table
once enough history has accumulated) can be swapped in without touching the
pipeline. That backtester is intentionally NOT built yet — this only leaves the
extension point clean.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

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

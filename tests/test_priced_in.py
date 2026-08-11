"""Tests for "already priced in" suppression.

Measured on 371 matured alerts: bullish calls where the stock had already run
>= +8% over the prior 5 sessions hit 22% with -3.09% avg alpha, vs 36% / -0.32%
below that line — and the effect held out of sample. See src/scoring/priced_in.py.

The guards matter as much as the rule: it must never fire on bearish calls (only
one matured bearish sample exists, so there is no evidence), and never on a
ticker with no price data (unpriceable is not evidence of being priced in).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.scoring.priced_in import is_priced_in, prior_move_pct
from src.storage.models import Base


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


_WHEN = datetime(2026, 8, 5, tzinfo=timezone.utc)


def _with_drift(value):
    return patch("src.scoring.priced_in.prior_move_pct", return_value=value)


def test_bullish_above_threshold_is_suppressed(session):
    with _with_drift(12.0):
        assert is_priced_in(
            session, ticker="X.NS", direction="bullish",
            published_at=_WHEN, threshold_pct=8.0,
        )


def test_bullish_below_threshold_still_alerts(session):
    with _with_drift(3.0):
        assert not is_priced_in(
            session, ticker="X.NS", direction="bullish",
            published_at=_WHEN, threshold_pct=8.0,
        )


def test_threshold_is_inclusive(session):
    with _with_drift(8.0):
        assert is_priced_in(
            session, ticker="X.NS", direction="bullish",
            published_at=_WHEN, threshold_pct=8.0,
        )


def test_bearish_is_never_suppressed_even_after_a_huge_run(session):
    # Only one matured bearish sample exists, so the symmetric case is
    # deliberately NOT assumed. A big run-up must not mute bad news.
    with _with_drift(25.0):
        assert not is_priced_in(
            session, ticker="X.NS", direction="bearish",
            published_at=_WHEN, threshold_pct=8.0,
        )


def test_unpriceable_ticker_is_not_treated_as_priced_in(session):
    # No price data must fail OPEN — otherwise every unpriceable scrip would be
    # silently muted by a data gap rather than by evidence.
    with _with_drift(None):
        assert not is_priced_in(
            session, ticker="NOPRICE.BO", direction="bullish",
            published_at=_WHEN, threshold_pct=8.0,
        )


def test_zero_threshold_disables_the_check(session):
    with _with_drift(50.0) as m:
        assert not is_priced_in(
            session, ticker="X.NS", direction="bullish",
            published_at=_WHEN, threshold_pct=0.0,
        )
    m.assert_not_called()  # and doesn't pay for a price fetch


def test_prior_move_falls_back_to_bhavcopy_when_yahoo_has_nothing(session):
    with patch("src.scoring.priced_in.get_prior_return", return_value=None), \
         patch("src.ingestion.bse_bhavcopy.get_prior_return", return_value=9.5) as bhav:
        assert prior_move_pct(session, "543254.BO", _WHEN) == 9.5
    bhav.assert_called_once()


def test_prior_move_prefers_yahoo_and_skips_bhavcopy(session):
    with patch("src.scoring.priced_in.get_prior_return", return_value=4.0), \
         patch("src.ingestion.bse_bhavcopy.get_prior_return") as bhav:
        assert prior_move_pct(session, "RELIANCE.NS", _WHEN) == 4.0
    bhav.assert_not_called()


def test_bhavcopy_failure_does_not_propagate(session):
    with patch("src.scoring.priced_in.get_prior_return", return_value=None), \
         patch("src.ingestion.bse_bhavcopy.get_prior_return",
               side_effect=ConnectionError("bhavcopy down")):
        assert prior_move_pct(session, "543254.BO", _WHEN) is None

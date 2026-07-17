"""Tests for src/scoring/outcomes.py: track_outcomes() records both the stock's
forward return AND the NIFTY 50 (^NSEI) forward return over the same window.

Raw stock return alone can't tell "our news call added value" apart from "the
whole market moved" — evaluate.py's alpha = ret - idx_ret needs both legs
recorded. The two fetches are independent (get_forward_return is called once
per ticker, once for "^NSEI"), so either can succeed or fail on its own; these
tests pin that independence down directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.scoring.outcomes import track_outcomes
from src.storage.db import save_article, mark_alert_sent
from src.storage.models import Base


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _add(session, ticker, published_days_ago=6):
    published_at = datetime.now(timezone.utc) - timedelta(days=published_days_ago)
    a = save_article(
        session, ticker=ticker, headline=f"{ticker} headline", url=f"u-{ticker}",
        source="nse_announcements", published_at=published_at, category="Acquisition",
        impact_tier="high", event_type="ma_deal", direction="bullish", confidence=0.7,
        materiality_score=0.7, impact_horizon="1_3_days", source_quality=1.0,
        is_material=True, reasoning="x",
    )
    mark_alert_sent(session, a.id)
    return a


def test_recording_stock_return_also_records_index_return(session):
    # Both legs fetched, both should land: ret_1d from the ticker call,
    # idx_ret_1d from the "^NSEI" call.
    a = _add(session, "RITES.NS")

    def fake_forward_return(ticker, published_at, tdays):
        if ticker == "^NSEI":
            return 1.0
        return 2.5

    with patch("src.scoring.outcomes.get_forward_return", side_effect=fake_forward_return):
        recorded = track_outcomes(session, limit=60)

    session.refresh(a)
    assert a.ret_1d == 2.5
    assert a.idx_ret_1d == 1.0
    assert recorded >= 2


def test_index_fetch_none_leaves_idx_null_but_keeps_stock_fill(session):
    a = _add(session, "RITES.NS")

    def fake_forward_return(ticker, published_at, tdays):
        if ticker == "^NSEI":
            return None
        return 2.5

    with patch("src.scoring.outcomes.get_forward_return", side_effect=fake_forward_return):
        track_outcomes(session, limit=60)

    session.refresh(a)
    assert a.ret_1d == 2.5
    assert a.idx_ret_1d is None


def test_stock_fetch_none_leaves_ret_null_but_still_tries_index(session):
    a = _add(session, "DEAD.NS")

    def fake_forward_return(ticker, published_at, tdays):
        if ticker == "^NSEI":
            return 1.0
        return None

    with patch("src.scoring.outcomes.get_forward_return", side_effect=fake_forward_return):
        track_outcomes(session, limit=60)

    session.refresh(a)
    assert a.ret_1d is None
    assert a.idx_ret_1d == 1.0


def test_idx_backfill_pass_fills_ret_present_idx_null_rows(session):
    # Simulates: an earlier run recorded the stock return but the index fetch
    # failed that time. A later run must pick up ONLY the missing idx leg,
    # not re-fetch (or clobber) the already-recorded stock return.
    # published 1 day ago: matured for the 1d horizon only, so only the 1d
    # horizon's query runs and the call list below is unambiguous.
    a = _add(session, "RITES.NS", published_days_ago=1)
    a.ret_1d = 2.5  # already recorded, idx_ret_1d still NULL
    session.commit()

    calls = []

    def fake_forward_return(ticker, published_at, tdays):
        calls.append(ticker)
        return 1.0

    with patch("src.scoring.outcomes.get_forward_return", side_effect=fake_forward_return):
        recorded = track_outcomes(session, limit=60)

    session.refresh(a)
    assert a.ret_1d == 2.5  # untouched
    assert a.idx_ret_1d == 1.0
    assert calls == ["^NSEI"]  # stock leg was NOT re-fetched
    assert recorded == 1


def test_not_yet_matured_alert_is_not_touched(session):
    a = _add(session, "RITES.NS", published_days_ago=0)

    with patch("src.scoring.outcomes.get_forward_return") as mock_fr:
        recorded = track_outcomes(session, limit=60)

    mock_fr.assert_not_called()
    assert recorded == 0
    session.refresh(a)
    assert a.ret_1d is None
    assert a.idx_ret_1d is None


def test_aged_past_tracking_window_is_not_retried(session):
    a = _add(session, "OLD.NS", published_days_ago=25)  # > _MAX_TRACK_AGE_DAYS

    with patch("src.scoring.outcomes.get_forward_return") as mock_fr:
        recorded = track_outcomes(session, limit=60)

    mock_fr.assert_not_called()
    assert recorded == 0

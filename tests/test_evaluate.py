"""Tests for evaluate.py's coverage and impact-rate math — the parts that make
'unmeasurable' visible instead of silently excluded, and separate 'did the news
move the stock at all' from 'did we call the direction right'."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from evaluate import IMPACT_MOVE_THRESHOLD_PCT, coverage_stats, impact_stats
from src.storage.db import save_article, mark_alert_sent
from src.storage.models import Base


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _add(session, ticker, ret_3d, published_days_ago=5):
    published_at = datetime.now(timezone.utc) - timedelta(days=published_days_ago)
    a = save_article(
        session, ticker=ticker, headline=f"{ticker} headline", url=f"u-{ticker}-{ret_3d}",
        source="nse_announcements", published_at=published_at, category="Acquisition",
        impact_tier="high", event_type="ma_deal", direction="bullish", confidence=0.7,
        materiality_score=0.7, impact_horizon="1_3_days", source_quality=1.0,
        is_material=True, reasoning="x",
    )
    mark_alert_sent(session, a.id)
    if ret_3d is not None:
        a.ret_3d = ret_3d
        session.commit()
    return a


# ── coverage_stats ──────────────────────────────────────────────────────────

def test_coverage_counts_measured_vs_unpriceable(session):
    _add(session, "RITES.NS", 3.2)
    _add(session, "544574.BO", None)  # never got a price -> permanently NULL
    _add(session, "543254.BO", None)

    cov = coverage_stats(session, "ret_3d")
    assert cov["total"] == 3
    assert cov["measured"] == 1
    assert set(cov["missing_tickers"]) == {"544574.BO", "543254.BO"}


def test_coverage_excludes_not_yet_matured_alerts(session):
    # Published 1 day ago; ret_3d needs >= 3 calendar days to be expected —
    # this alert isn't "should have data yet" and must not count against coverage.
    _add(session, "FRESH.NS", None, published_days_ago=1)

    cov = coverage_stats(session, "ret_3d")
    assert cov["total"] == 0


def test_coverage_only_counts_alerted_rows(session):
    a = save_article(
        session, ticker="X.NS", headline="x", url="u1", source="nse_announcements",
        published_at=datetime.now(timezone.utc) - timedelta(days=5), category="Acquisition",
        impact_tier="high", event_type="ma_deal", direction="bullish", confidence=0.7,
        materiality_score=0.7, impact_horizon="1_3_days", source_quality=1.0,
        is_material=True, reasoning="x",
    )
    # never marked alert_sent

    cov = coverage_stats(session, "ret_3d")
    assert cov["total"] == 0


def test_coverage_100_percent_when_all_measured(session):
    _add(session, "A.NS", 1.5)
    _add(session, "B.NS", -0.5)
    cov = coverage_stats(session, "ret_3d")
    assert cov["total"] == cov["measured"] == 2
    assert cov["missing_tickers"] == []


# ── impact_stats ─────────────────────────────────────────────────────────────

def test_impact_stats_avg_and_rate():
    # threshold is 2.0%; 3.0 and 4.0 are impactful, 1.0 is not.
    avg_abs, rate = impact_stats([3.0, -4.0, 1.0])
    assert avg_abs == pytest.approx((3.0 + 4.0 + 1.0) / 3)
    assert rate == pytest.approx(2 / 3)


def test_impact_stats_uses_absolute_value_direction_agnostic():
    # a -5% move is just as impactful as a +5% move
    avg_abs, rate = impact_stats([-5.0])
    assert avg_abs == 5.0
    assert rate == 1.0


def test_impact_stats_empty_list_returns_zeros():
    assert impact_stats([]) == (0.0, 0.0)


def test_impact_stats_threshold_is_inclusive():
    avg_abs, rate = impact_stats([IMPACT_MOVE_THRESHOLD_PCT])
    assert rate == 1.0


def test_impact_stats_below_threshold_not_counted():
    avg_abs, rate = impact_stats([IMPACT_MOVE_THRESHOLD_PCT - 0.01])
    assert rate == 0.0

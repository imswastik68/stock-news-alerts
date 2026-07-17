"""Tests for evaluate.py's coverage and impact-rate math — the parts that make
'unmeasurable' visible instead of silently excluded, and separate 'did the news
move the stock at all' from 'did we call the direction right'."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from evaluate import IMPACT_MOVE_THRESHOLD_PCT, alpha_of, coverage_stats, impact_stats, wilson_ci
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


def _add_n(session, ticker, ret_3d, seq, published_days_ago=5):
    """Like _add but with an explicit sequence number so the same ticker can
    have multiple rows (the real system alerts the same ticker repeatedly —
    TATACAP.NS, SOMANYCERA.NS, WELCORP.NS each got 2-3 separate alerts)."""
    published_at = datetime.now(timezone.utc) - timedelta(days=published_days_ago)
    a = save_article(
        session, ticker=ticker, headline=f"{ticker} headline {seq}", url=f"u-{ticker}-{seq}",
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


def test_coverage_counts_rows_not_unique_tickers_when_same_ticker_repeats(session):
    # Regression: `measured = total - len(unique_missing_tickers)` undercounts
    # missing rows (and so overcounts measured) whenever a ticker has BOTH a
    # measured and an unmeasured alert. Reproduced directly: 2 unmeasured rows
    # for X.NS + 2 measured rows for Y.NS reported "measured: 3" instead of 2.
    _add_n(session, "X.NS", None, 1)
    _add_n(session, "X.NS", None, 2)
    _add_n(session, "Y.NS", 5.0, 1)
    _add_n(session, "Y.NS", 3.0, 2)

    cov = coverage_stats(session, "ret_3d")
    assert cov["total"] == 4
    assert cov["measured"] == 2  # NOT 3
    assert cov["missing_tickers"] == ["X.NS"]


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


# ── wilson_ci ────────────────────────────────────────────────────────────────

def test_wilson_ci_known_values():
    # Reference values (standard Wilson score interval, z=1.96), cross-checked
    # against this module's own live verification during implementation.
    lo, hi = wilson_ci(1, 10)
    assert lo == pytest.approx(0.018, abs=0.001)
    assert hi == pytest.approx(0.404, abs=0.001)

    lo, hi = wilson_ci(0, 2)
    assert lo == pytest.approx(0.0, abs=0.001)
    assert hi == pytest.approx(0.658, abs=0.001)

    lo, hi = wilson_ci(5, 10)
    assert lo == pytest.approx(0.237, abs=0.001)
    assert hi == pytest.approx(0.763, abs=0.001)

    lo, hi = wilson_ci(10, 10)
    assert lo == pytest.approx(0.722, abs=0.001)
    assert hi == pytest.approx(1.0, abs=0.001)


def test_wilson_ci_zero_n_is_maximally_uninformative():
    assert wilson_ci(0, 0) == (0.0, 1.0)


def test_wilson_ci_small_n_interval_is_wide():
    # The exact motivation for adding this: a 2-sample "0%" bucket must not
    # read like a confident result — its interval should span most of [0, 1].
    lo, hi = wilson_ci(0, 2)
    assert hi - lo > 0.5


# ── alpha_of ─────────────────────────────────────────────────────────────────

def test_alpha_of_is_ret_minus_index_ret():
    assert alpha_of(3.0, 1.0) == pytest.approx(2.0)
    assert alpha_of(-1.0, -3.0) == pytest.approx(2.0)


def test_alpha_of_none_when_index_leg_missing():
    assert alpha_of(3.0, None) is None


def test_alpha_can_flip_a_raw_hit_into_a_miss():
    # The whole point of alpha: a stock that rose 1% while the market rose 2%
    # underperformed the market by 1%, even though the raw return was positive.
    ret, idx_ret = 1.0, 2.0
    alpha = alpha_of(ret, idx_ret)
    raw_hit = ret > 0  # bullish call, raw return positive -> raw "hit"
    alpha_hit = alpha > 0  # but alpha is negative -> alpha "miss"
    assert raw_hit is True
    assert alpha_hit is False


def test_alpha_can_flip_a_raw_miss_into_a_hit():
    # Symmetric case: stock fell 1% but the market fell 3% -> alpha +2%, so a
    # bullish call that looks like a raw miss is actually a relative win.
    ret, idx_ret = -1.0, -3.0
    alpha = alpha_of(ret, idx_ret)
    raw_hit = ret > 0
    alpha_hit = alpha > 0
    assert raw_hit is False
    assert alpha_hit is True


# ── alpha rows: NULL idx excluded from alpha stats but kept in raw ──────────

def _add_with_idx(session, ticker, ret_3d, idx_ret_3d, seq=1, published_days_ago=5):
    published_at = datetime.now(timezone.utc) - timedelta(days=published_days_ago)
    a = save_article(
        session, ticker=ticker, headline=f"{ticker} headline {seq}", url=f"u-{ticker}-{seq}",
        source="nse_announcements", published_at=published_at, category="Acquisition",
        impact_tier="high", event_type="ma_deal", direction="bullish", confidence=0.7,
        materiality_score=0.7, impact_horizon="1_3_days", source_quality=1.0,
        is_material=True, reasoning="x",
    )
    mark_alert_sent(session, a.id)
    a.ret_3d = ret_3d
    a.idx_ret_3d = idx_ret_3d
    session.commit()
    return a


def test_rows_with_null_idx_ret_kept_for_raw_but_excluded_from_alpha(session):
    from evaluate import _rows

    _add_with_idx(session, "A.NS", 3.0, 1.0, seq=1)   # has idx leg
    _add_with_idx(session, "B.NS", 2.0, None, seq=2)  # idx leg missing

    rows = _rows(session, "ret_3d")
    assert len(rows) == 2  # both counted in raw stats

    alpha_rows = [(d, alpha_of(r, idx)) for _, _, d, _, r, idx in rows if idx is not None]
    assert len(alpha_rows) == 1  # only the one with a recorded index leg

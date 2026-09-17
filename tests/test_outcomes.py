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

from src.scoring.outcomes import entry_basis_for, track_outcomes, track_shadow_outcomes
from src.storage.db import save_article, mark_alert_sent
from src.storage.models import Base

# NSE closes 15:30 IST = 10:00 UTC. These two hours put a filing unambiguously
# inside / after the session, so the entry basis a test exercises is fixed
# rather than depending on what time of day the suite happens to run.
_INTRADAY_UTC_HOUR = 6   # 11:30 IST — session open, basis "close"
_AFTER_CLOSE_UTC_HOUR = 14  # 19:30 IST — basis "next_open"


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _add(session, ticker, published_days_ago=6, utc_hour=_INTRADAY_UTC_HOUR):
    published_at = (datetime.now(timezone.utc) - timedelta(days=published_days_ago)).replace(
        hour=utc_hour, minute=0, second=0, microsecond=0
    )
    if utc_hour == _AFTER_CLOSE_UTC_HOUR:
        # An after-close filing is only "next_open" on a trading day — on a
        # weekend the next close is Monday's and already post-news. Step back to
        # a weekday so the intended basis is the one under test.
        while published_at.weekday() >= 5:
            published_at -= timedelta(days=1)
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


# ── entry basis: can a trader actually get the price we measured from? ───────
#
# A filing released after 15:30 IST is followed by a close that printed BEFORE
# the news existed. Measuring from it books the overnight gap as alpha even
# though no order could have been filled at that base. Measured on 271
# delivered alerts: the after-hours bucket showed +1.75% avg 1d alpha on a close
# base against +0.26% for genuinely tradable rows.

def test_intraday_filing_measures_from_that_days_close():
    dt = datetime(2026, 9, 16, 6, 0, tzinfo=timezone.utc)  # Wed 11:30 IST
    assert entry_basis_for(dt) == "close"


def test_after_close_filing_measures_from_the_next_open():
    dt = datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc)  # Wed 19:30 IST
    assert entry_basis_for(dt) == "next_open"


def test_filing_exactly_at_the_bell_still_counts_as_intraday():
    dt = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)  # Wed 15:30 IST sharp
    assert entry_basis_for(dt) == "close"


def test_weekend_filing_is_close_basis_because_mondays_close_is_post_news():
    dt = datetime(2026, 9, 19, 14, 0, tzinfo=timezone.utc)  # Saturday
    assert entry_basis_for(dt) == "close"


def test_naive_published_at_is_treated_as_utc_not_local():
    # SQLite hands back naive datetimes. Guessing local time here would flip the
    # basis on any machine not running in UTC.
    assert entry_basis_for(datetime(2026, 9, 16, 14, 0)) == "next_open"


def test_after_close_alert_uses_the_open_fetcher_and_records_the_basis(session):
    a = _add(session, "RITES.NS", utc_hour=_AFTER_CLOSE_UTC_HOUR)

    def from_open(ticker, published_at, tdays):
        return 1.0 if ticker == "^NSEI" else 2.5

    with patch("src.scoring.outcomes.get_forward_return_from_open", side_effect=from_open), \
         patch("src.scoring.outcomes.get_forward_return") as close_fetch:
        track_outcomes(session, limit=60)

    session.refresh(a)
    assert a.ret_1d == 2.5
    assert a.idx_ret_1d == 1.0           # index on the SAME basis, not the close
    assert a.entry_basis == "next_open"
    close_fetch.assert_not_called()      # the contaminated base was never touched


def test_intraday_alert_records_close_basis(session):
    a = _add(session, "RITES.NS")

    with patch("src.scoring.outcomes.get_forward_return", side_effect=lambda t, p, d: 1.0):
        track_outcomes(session, limit=60)

    session.refresh(a)
    assert a.entry_basis == "close"


def test_bhavcopy_downgrades_next_open_to_next_close_and_index_follows(session):
    # BSE bhavcopy stores EOD closes only. Entering at the next session's close
    # is still post-news, so it stays honest — but the index leg has to move to
    # the same window or alpha subtracts two different exposures.
    a = _add(session, "532933.BO", utc_hour=_AFTER_CLOSE_UTC_HOUR)
    calls = []

    def close_fetch(ticker, from_dt, tdays):
        calls.append((ticker, from_dt))
        return 1.0

    with patch("src.scoring.outcomes.get_forward_return_from_open", return_value=None), \
         patch("src.scoring.outcomes.bse_bhavcopy.get_forward_return", return_value=2.5), \
         patch("src.scoring.outcomes.get_forward_return", side_effect=close_fetch):
        track_outcomes(session, limit=60)

    session.refresh(a)
    assert a.ret_1d == 2.5
    assert a.entry_basis == "next_close"
    # Every leg — index and the later horizons' stock legs — starts from the day
    # AFTER publication. A single call left on the publication date would be the
    # pre-news close this basis exists to avoid.
    next_day = a.published_at.date() + timedelta(days=1)
    assert calls
    assert all(d == next_day for _, d in calls)
    assert "^NSEI" in [t for t, _ in calls]


def test_stored_basis_wins_over_a_freshly_derived_one_on_the_backfill_pass(session):
    # The stock leg resolved to next_close on an earlier run. The later index-only
    # pass must reuse that, not re-derive "next_open" and fetch a mismatched window.
    a = _add(session, "532933.BO", published_days_ago=1, utc_hour=_AFTER_CLOSE_UTC_HOUR)
    a.ret_1d = 2.5
    a.entry_basis = "next_close"
    session.commit()

    with patch("src.scoring.outcomes.get_forward_return_from_open") as open_fetch, \
         patch("src.scoring.outcomes.get_forward_return", side_effect=lambda t, p, d: 1.0):
        track_outcomes(session, limit=60)

    session.refresh(a)
    assert a.idx_ret_1d == 1.0
    open_fetch.assert_not_called()


# --- Shadow tracking -------------------------------------------------------
#
# Filings we classify but deliberately don't alert on (a blocked event type, or
# one that misses min_materiality_score) never set alert_sent, so track_outcomes
# never measured them. That made ROADMAP P1.9/P1.10 unanswerable by waiting:
# the evidence needed to re-open those decisions was the evidence not being
# collected. track_shadow_outcomes() measures them WITHOUT making them alerts.


def _add_unalerted(session, ticker, event_type="credit_rating", direction="bearish",
                   published_days_ago=6):
    published_at = (datetime.now(timezone.utc) - timedelta(days=published_days_ago)).replace(
        hour=_INTRADAY_UTC_HOUR, minute=0, second=0, microsecond=0
    )
    return save_article(
        session, ticker=ticker, headline=f"{ticker} headline", url=f"u-{ticker}",
        source="nse_announcements", published_at=published_at, category="Rating",
        impact_tier="high", event_type=event_type, direction=direction, confidence=0.7,
        materiality_score=0.7, impact_horizon="1_3_days", source_quality=1.0,
        is_material=True, reasoning="x",
    )


def test_a_blocked_event_type_is_never_measured_by_the_alerted_pass(session):
    # The regression this exists for: credit_rating is directionally blocked, so
    # alert_sent stays False and the normal pass cannot see it at all.
    a = _add_unalerted(session, "RATED.NS")

    with patch("src.scoring.outcomes.get_forward_return", side_effect=lambda t, p, d: 2.0):
        track_outcomes(session, limit=60)

    session.refresh(a)
    assert a.ret_1d is None


def test_shadow_tracking_measures_the_filings_we_chose_not_to_alert_on(session):
    a = _add_unalerted(session, "RATED.NS")

    def fake(ticker, published_at, tdays):
        return 1.0 if ticker == "^NSEI" else -3.0

    with patch("src.scoring.outcomes.get_forward_return", side_effect=fake):
        recorded = track_shadow_outcomes(session, limit=40)

    session.refresh(a)
    assert a.ret_1d == -3.0
    assert a.idx_ret_1d == 1.0
    assert recorded >= 2


def test_shadow_tracking_does_not_turn_the_row_into_an_alert(session):
    # The whole safety argument rests on alert_sent staying False: that is what
    # keeps these rows out of calibration and out of the reported track record.
    a = _add_unalerted(session, "RATED.NS")

    with patch("src.scoring.outcomes.get_forward_return", side_effect=lambda t, p, d: 2.0):
        track_shadow_outcomes(session, limit=40)

    session.refresh(a)
    assert a.alert_sent is False
    assert a.ret_1d is not None


def test_shadow_measured_rows_stay_out_of_the_calibration_stats(session):
    # get_hit_rate_stats feeds BacktestedConfidenceProvider, which sets the
    # confidence that decides what alerts. If a shadow row leaked in here it
    # would change live alerting behaviour as a side effect of measurement.
    from src.storage.db import get_hit_rate_stats

    a = _add_unalerted(session, "RATED.NS")
    a.ret_3d = -5.0
    session.commit()

    assert get_hit_rate_stats(session, horizon="ret_3d") == {}


def test_shadow_tracking_skips_neutral_and_unclassified_rows(session):
    # A neutral row makes no directional claim, so there is nothing to score it
    # against; a failed classification has no claim at all.
    neutral = _add_unalerted(session, "NEUT.NS", direction="neutral")
    failed = _add_unalerted(session, "FAIL.NS", event_type="classification_failed")

    with patch("src.scoring.outcomes.get_forward_return", side_effect=lambda t, p, d: 2.0):
        track_shadow_outcomes(session, limit=40)

    session.refresh(neutral)
    session.refresh(failed)
    assert neutral.ret_1d is None
    assert failed.ret_1d is None


def test_shadow_tracking_leaves_already_alerted_rows_to_the_normal_pass(session):
    # No double-counting: the two passes must partition the rows, not overlap.
    alerted = _add(session, "SENT.NS")
    fetched = []

    def fake(ticker, published_at, tdays):
        fetched.append(ticker)
        return 2.0

    with patch("src.scoring.outcomes.get_forward_return", side_effect=fake):
        track_shadow_outcomes(session, limit=40)

    session.refresh(alerted)
    assert alerted.ret_1d is None
    assert "SENT.NS" not in fetched


def test_shadow_tracking_takes_the_newest_rows_first(session):
    # A permanent backlog of unpriceable old scrips must not be able to consume
    # the budget every cycle and starve today's measurable filings.
    _add_unalerted(session, "OLD.NS", published_days_ago=18)
    _add_unalerted(session, "NEW.NS", published_days_ago=2)
    fetched = []

    def fake(ticker, published_at, tdays):
        fetched.append(ticker)
        return None  # nothing is priceable, so nothing gets written off

    with patch("src.scoring.outcomes.get_forward_return", side_effect=fake):
        track_shadow_outcomes(session, limit=1)

    assert fetched and fetched[0] == "NEW.NS"

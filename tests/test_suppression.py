"""A withheld alert must never count as an alert.

Suppressed duplicates and priced-in rows are marked alert_sent so the pending
queue never retries them. That one flag was also what evaluate.py and the
confidence calibration read as "this alert went out", so rows nobody ever
received were being scored as predictions.

It mattered: on 2026-09-17, 106 of 377 alert_sent rows (28%) had no matching
Telegram message. Those undelivered rows hit 47.8% at 1d against 64.4% for
delivered ones, so they dragged the reported track record down ~5 points and
fed the same noise into the per-event-type priors.

A suppressed duplicate's forward return is measured from a base AFTER the move
it duplicates has already happened, so it carries no directional information by
construction — this isn't a sampling quirk that a larger n would fix.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from evaluate import _rows as alert_rows, coverage_stats
from src.storage.db import (
    get_hit_rate_stats,
    get_pending_alert_articles,
    mark_alert_sent,
    mark_alert_suppressed,
    save_article,
)
from src.storage.models import Base


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _add(session, ticker, ret_1d=None, days_ago=5, direction="bullish"):
    a = save_article(
        session, ticker=ticker, headline=f"{ticker} headline", url=f"u-{ticker}",
        source="nse_announcements",
        published_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        category="Acquisition", impact_tier="high", event_type="partnership_contract",
        direction=direction, confidence=0.5, materiality_score=0.8,
        impact_horizon="1_3_days", source_quality=1.0, is_material=True, reasoning="x",
    )
    if ret_1d is not None:
        a.ret_1d = ret_1d
        a.idx_ret_1d = 0.0
        session.commit()
    return a


# ── the flag itself ─────────────────────────────────────────────────────────

def test_suppressed_row_is_marked_sent_so_it_is_never_retried(session):
    a = _add(session, "AAA.NS")
    mark_alert_suppressed(session, a.id, "duplicate")
    session.refresh(a)
    assert a.alert_sent is True
    assert a.suppressed_reason == "duplicate"


def test_a_really_sent_alert_has_no_suppression_reason(session):
    a = _add(session, "AAA.NS")
    mark_alert_sent(session, a.id)
    session.refresh(a)
    assert a.alert_sent is True
    assert a.suppressed_reason is None


def test_suppressed_row_stays_out_of_the_pending_queue(session):
    # The reason alert_sent is set at all — a withheld alert must not come back
    # round on the next cycle and get sent after the fact.
    a = _add(session, "AAA.NS", days_ago=0)
    mark_alert_suppressed(session, a.id, "priced_in")
    pending = get_pending_alert_articles(
        session, confidence_threshold=0.0, min_source_quality=0.0,
        min_published_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    assert [p.id for p in pending] == []


# ── measurement must exclude them ───────────────────────────────────────────

def test_evaluate_ignores_suppressed_rows(session):
    delivered = _add(session, "AAA.NS", ret_1d=3.0)
    mark_alert_sent(session, delivered.id)
    withheld = _add(session, "BBB.NS", ret_1d=-9.0)
    mark_alert_suppressed(session, withheld.id, "duplicate")

    rows = alert_rows(session, "ret_1d")
    assert len(rows) == 1
    assert rows[0][4] == 3.0


def test_coverage_does_not_count_suppressed_rows_as_unpriceable(session):
    # A withheld alert with no price data is not a coverage gap — it was never
    # an alert. Counting it would overstate how much we fail to measure.
    delivered = _add(session, "AAA.NS", ret_1d=1.0)
    mark_alert_sent(session, delivered.id)
    withheld = _add(session, "BBB.NS", ret_1d=None)
    mark_alert_suppressed(session, withheld.id, "priced_in")

    cov = coverage_stats(session, "ret_1d")
    assert (cov["total"], cov["measured"]) == (1, 1)
    assert cov["missing_tickers"] == []


def test_confidence_calibration_does_not_learn_from_suppressed_rows(session):
    # get_hit_rate_stats feeds the per-event-type priors. Three withheld losers
    # must not turn one delivered winner into a 25% event type.
    won = _add(session, "AAA.NS", ret_1d=4.0)
    mark_alert_sent(session, won.id)
    for i, t in enumerate(("BBB.NS", "CCC.NS", "DDD.NS")):
        lost = _add(session, t, ret_1d=-4.0)
        mark_alert_suppressed(session, lost.id, "duplicate")

    stats = get_hit_rate_stats(session, "ret_1d")
    assert stats["partnership_contract"] == {"n": 1, "hits": 1}

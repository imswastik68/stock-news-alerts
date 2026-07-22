"""Tests for cross-source/cross-exchange alert-duplicate suppression.

Thresholds calibrated on REAL pairs pulled from production Telegram alerts (see
the module docstring in src/scoring/dedup.py): a confirmed cross-exchange
duplicate (same acquisition, alerted once as 'TATACAP.NS' via NSE and once as
'544574.BO' via BSE, 18 seconds apart) scored 0.85 similarity; a BSE re-filing
of the same disclosure 7 minutes later (typo-corrected) scored 0.98; genuinely
new information on an evolving deal (entity count/amount changed) scored as low
as 0.28-0.42. 0.65 sits cleanly between them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.scoring.dedup import is_duplicate_of_recent_alert
from src.storage.models import Article, Base


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _add_sent_alert(
    session, headline: str, direction: str = "bullish", created_at=None,
    ticker: str = "X", event_type: str = "partnership_contract",
) -> None:
    a = Article(
        ticker=ticker, headline=headline, headline_hash="h" + headline[:10],
        url=f"u-{ticker}-{headline[:20]}",
        source="nse_announcements", published_at=datetime.now(timezone.utc),
        event_type=event_type, direction=direction, confidence=0.8,
        reasoning="x", alert_sent=True,
        created_at=created_at or datetime.now(timezone.utc),
    )
    session.add(a)
    session.commit()


def test_real_cross_exchange_duplicate_is_caught(session):
    _add_sent_alert(session, "Acquires 88.6% stake in Yogakshemam Loans Limited")
    assert is_duplicate_of_recent_alert(
        session,
        headline="Tata Capital to acquire 88.6% stake in Yogakshemam Loans Limited",
        direction="bullish",
    )


def test_real_refiling_duplicate_is_caught(session):
    _add_sent_alert(
        session,
        "Opens new manufacturing plant in Coimbatore, to enhance production capacity by 60,000 units per annum",
    )
    assert is_duplicate_of_recent_alert(
        session,
        headline="Opens new manufacturing plant at Coimbatore, to enhance production capacity by 60,000 units per annum",
        direction="bullish",
    )


def test_materially_new_information_not_suppressed(session):
    # Deal terms changed (different amount, different entity count) — genuinely
    # new information, must still alert.
    _add_sent_alert(session, "To invest up to Rs 58.80 cr in Siravit Ceramics, up to Rs 2 cr in V.S. Industries")
    assert not is_duplicate_of_recent_alert(
        session,
        headline="Accords in-principle approval for investments of up to Rs 75.80 cr in two entities",
        direction="bullish",
    )


def test_unrelated_headlines_not_suppressed(session):
    _add_sent_alert(session, "Board recommends Rs 229 final dividend")
    assert not is_duplicate_of_recent_alert(
        session,
        headline="Wins Rs 79.22 cr general consultancy order from Patna Metro Rail Corporation",
        direction="bullish",
    )


def test_opposite_direction_not_suppressed_even_if_textually_similar(session):
    _add_sent_alert(session, "Reports FY26 profit up 29%", direction="bullish")
    assert not is_duplicate_of_recent_alert(
        session, headline="Reports FY26 profit up 29%", direction="bearish"
    )


def test_outside_window_not_suppressed(session):
    old = datetime.now(timezone.utc) - timedelta(hours=10)
    _add_sent_alert(session, "Acquires 88.6% stake in Yogakshemam Loans Limited", created_at=old)
    assert not is_duplicate_of_recent_alert(
        session,
        headline="Tata Capital to acquire 88.6% stake in Yogakshemam Loans Limited",
        direction="bullish",
        window_hours=3.0,
    )


def test_only_alert_sent_rows_considered(session):
    # A stored-but-never-sent article (e.g. classification_failed, or filtered
    # out) must not suppress a genuinely new alert.
    a = Article(
        ticker="X", headline="Acquires 88.6% stake in Yogakshemam Loans Limited",
        headline_hash="h1", url="u1", source="nse_announcements",
        published_at=datetime.now(timezone.utc), event_type="ma_deal", direction="bullish",
        confidence=0.8, reasoning="x", alert_sent=False,
    )
    session.add(a)
    session.commit()
    assert not is_duplicate_of_recent_alert(
        session,
        headline="Tata Capital to acquire 88.6% stake in Yogakshemam Loans Limited",
        direction="bullish",
    )


def test_empty_headline_never_flagged_duplicate(session):
    _add_sent_alert(session, "Some alert")
    assert not is_duplicate_of_recent_alert(session, headline="", direction="bullish")


# ── same ticker + same event_type = same event, whatever the wording ─────────
# The case that forced this rule: NESTLEIND's Q1 results went out THREE times in
# 13 minutes, each describing a different metric from the one filing. Pairwise
# similarity was 0.58 / 0.18 / 0.21 — all under threshold — and every headline
# carried figures, so a narrower "figure-free restatement" rule missed them too.

_NESTLE = [
    "Reports Q1 net profit of Rs 9,751.2 cr, up from Rs 6,592.3 cr YoY",
    "Reports Q1 FY27 net profit up 48% YoY to Rs 975.1 cr",
    "Nestle India reports 25.4% sales growth in Q1",
]


@pytest.mark.parametrize("second", _NESTLE[1:])
def test_real_nestle_triplicate_is_suppressed(session, second):
    _add_sent_alert(session, _NESTLE[0], ticker="NESTLEIND.NS", event_type="earnings_surprise")
    assert is_duplicate_of_recent_alert(
        session, headline=second, direction="bullish",
        ticker="NESTLEIND.NS", event_type="earnings_surprise",
    )


def test_figure_free_restatement_still_suppressed(session):
    # The earlier CHAVDA/WELCORP/ARIS case stays covered by the same rule.
    _add_sent_alert(
        session, "Wins Rs 89.45 cr order from ADI Shantigram Abode LLP",
        ticker="CHAVDA.NS", event_type="partnership_contract",
    )
    assert is_duplicate_of_recent_alert(
        session, headline="Wins new order", direction="bullish",
        ticker="CHAVDA.NS", event_type="partnership_contract",
    )


def test_same_event_type_on_a_different_ticker_still_alerts(session):
    # Two different companies reporting earnings must both alert — the rule
    # keys on the ticker, so this is the guard that keeps it from over-firing.
    _add_sent_alert(session, _NESTLE[0], ticker="NESTLEIND.NS", event_type="earnings_surprise")
    assert not is_duplicate_of_recent_alert(
        session, headline="Reports Q1 profit up 12% YoY", direction="bullish",
        ticker="HCLTECH.NS", event_type="earnings_surprise",
    )


def test_different_event_type_on_the_same_ticker_still_alerts(session):
    # Results in the morning and an order win in the afternoon are two genuine
    # events for one company; only the event_type separates them.
    _add_sent_alert(session, _NESTLE[0], ticker="NESTLEIND.NS", event_type="earnings_surprise")
    assert not is_duplicate_of_recent_alert(
        session, headline="Wins Rs 400 cr supply contract", direction="bullish",
        ticker="NESTLEIND.NS", event_type="partnership_contract",
    )


def test_same_event_suppression_expires_after_its_window(session):
    old = datetime.now(timezone.utc) - timedelta(hours=30)  # > 24h window
    _add_sent_alert(
        session, _NESTLE[0], created_at=old,
        ticker="NESTLEIND.NS", event_type="earnings_surprise",
    )
    assert not is_duplicate_of_recent_alert(
        session, headline=_NESTLE[1], direction="bullish",
        ticker="NESTLEIND.NS", event_type="earnings_surprise",
    )


def test_same_event_path_requires_ticker_and_event_type(session):
    # Callers passing neither fall back to text similarity alone.
    _add_sent_alert(
        session, "Wins Rs 89.45 cr order from ADI Shantigram Abode LLP",
        ticker="CHAVDA.NS", event_type="partnership_contract",
    )
    assert not is_duplicate_of_recent_alert(
        session, headline="Wins new order", direction="bullish"
    )

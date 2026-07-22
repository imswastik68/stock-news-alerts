"""
Cross-source/cross-exchange alert-duplicate suppression.

The URL/headline-hash dedup in src/storage/db.py stops the exact same filing
being processed twice, but it can't catch the same real-world event surfacing
as genuinely different filings — a dual-listed company disclosing the same deal
to NSE and BSE separately (different tickers, different URLs, ~seconds apart),
or an exchange re-filing an announcement minutes later with a typo fix (a new
URL, same substance). Confirmed live: "TATACAP.NS: Acquires 88.6% stake in
Yogakshemam Loans" and "544574.BO: Tata Capital to acquire 88.6% stake in
Yogakshemam Loans" landed 18 seconds apart as two alerts for one acquisition.

This compares a candidate alert's LLM-generated headline against recently-SENT
alerts (same direction, within a short window) using stdlib difflib — no extra
dependency. Calibrated on real duplicate/non-duplicate pairs from production
alerts (see tests/test_dedup.py): genuine duplicates scored 0.75-0.98 similarity,
materially different updates on the same evolving story scored 0.28-0.42, so a
0.65 threshold separates them cleanly.

Text similarity alone cannot catch a whole class of real duplicates, because the
same filing can be *described* completely differently each time it re-surfaces
(the NSE-RSS best-effort copy vs the BSE copy, or a re-filing). Two live cases:

  - Figure-free restatements: "Wins Rs 89.45 cr order from ADI Shantigram Abode
    LLP" then simply "Wins new order" — 0.24-0.40 similarity.
  - Different metric each time: NESTLEIND's Q1 results went out THREE times in
    13 minutes as "Reports Q1 net profit of Rs 9,751.2 cr...", "Reports Q1 FY27
    net profit up 48% YoY to Rs 975.1 cr", and "Nestle India reports 25.4% sales
    growth in Q1" — pairwise similarity 0.58 / 0.18 / 0.21, all under threshold.

An earlier version of this module tried to target the first case narrowly (same
ticker + event_type AND a headline with no digit). NESTLEIND shows why that was
too narrow: every one of those three headlines carries figures, so none matched.

The rule now is the simpler and stronger one: for the SAME ticker, the SAME
event_type and the same direction, a second alert inside the window is the same
event. A company reports Q1 earnings once; it does not have three different
bullish earnings surprises in a quarter-hour. That window is deliberately longer
than the text-similarity one (a re-filing can land hours later) and is what
finally makes the ticker, not the wording, the thing dedup keys on.

The trade-off is accepted deliberately: a genuinely distinct second event of the
same type on the same ticker inside the window is suppressed. That is rare, and
one missed follow-up costs far less than the repeated alerts it prevents.
"""

from __future__ import annotations

import difflib
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.storage.models import Article

DEFAULT_WINDOW_HOURS = 3.0
DEFAULT_SIMILARITY_THRESHOLD = 0.65
# Longer than the text window on purpose: a re-filing or a second source's copy
# of the same results/order can land hours after the first, well outside the
# 3h text window (NESTLEIND's three copies spanned 13 minutes, but nothing
# guarantees that). Same ticker + same event_type is a strong enough signal to
# justify looking back a full day.
DEFAULT_SAME_EVENT_WINDOW_HOURS = 24.0


def _normalize(text: str) -> str:
    return " ".join((text or "").lower().split())


def _as_utc(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes even for timezone=True columns, so
    normalize before any Python-side comparison."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def is_duplicate_of_recent_alert(
    session: Session,
    *,
    headline: str,
    direction: str,
    ticker: str | None = None,
    event_type: str | None = None,
    window_hours: float = DEFAULT_WINDOW_HOURS,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    same_event_window_hours: float = DEFAULT_SAME_EVENT_WINDOW_HOURS,
) -> bool:
    """True if a similar-enough alert (same direction, sent within the last
    window_hours) already went out. Compares the clean LLM headline, not the
    raw filing text — boilerplate like 'has informed the Exchange about...' is
    near-identical across unrelated filings and would cause false positives.

    Two independent suppression paths, either one triggering a duplicate:
      1. Text similarity >= threshold within window_hours — catches copies of the
         same wording, including across tickers/exchanges.
      2. Same ticker + same event_type within same_event_window_hours, whatever
         the wording. This is what catches a filing the LLM described three
         different ways (see the module docstring). Requires both ticker and
         event_type; callers that pass neither get path 1 only.

    Anchored on OUR OWN send time (Article.created_at, i.e. "now"), not the
    exchange's published_at: the pipeline processes each cycle's articles
    HIGH-impact-first / newest-within-tier-first, not in strict published_at
    order, so a filing with an earlier published_at can be evaluated AFTER a
    later-published duplicate has already been sent. Anchoring on wall-clock
    send time is always monotonic with actual processing order and can't miss
    a duplicate that was sent moments ago just because its filing timestamp
    happens to be later."""
    if not headline or not headline.strip():
        return False

    now = datetime.now(timezone.utc)
    text_cutoff = now - timedelta(hours=window_hours)
    same_event_cutoff = now - timedelta(hours=same_event_window_hours)
    # Query spans whichever window reaches further back; each rule then applies
    # its own cutoff below.
    stmt = select(
        Article.headline, Article.ticker, Article.event_type, Article.created_at
    ).where(
        Article.alert_sent == True,  # noqa: E712
        Article.direction == direction,
        Article.created_at >= min(text_cutoff, same_event_cutoff),
    )
    norm_new = _normalize(headline)
    same_event_checkable = bool(ticker and event_type)
    for existing_headline, existing_ticker, existing_event_type, created_at in session.execute(stmt):
        created = _as_utc(created_at)
        if (
            same_event_checkable
            and existing_ticker == ticker
            and existing_event_type == event_type
            and created is not None
            and created >= same_event_cutoff
        ):
            return True
        if (
            created is not None
            and created >= text_cutoff
            and difflib.SequenceMatcher(None, norm_new, _normalize(existing_headline)).ratio() >= threshold
        ):
            return True
    return False

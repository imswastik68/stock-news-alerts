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

Text similarity alone has one blind spot, confirmed live: the SAME order/deal
re-surfaces (NSE-RSS best-effort copy vs BSE copy of one filing) and the LLM
distils the thinner copy into a CONTENT-FREE headline — "Wins new order",
"bags new order", "Wins order". Against the specific first alert ("Wins Rs 89.45
cr order from ADI Shantigram Abode LLP") those score only 0.24-0.40, well under
threshold, so a second, information-free alert went out — seen for CHAVDA,
WELCORP, ARIS, GP Eco, Happy Square. So beyond text similarity we also suppress
a candidate that (a) shares ticker + direction + event_type with a recent sent
alert AND (b) carries no digit at all — a restatement with no figure adds nothing
over the alert already sent for that same event. Crucially this leaves genuinely
evolving updates alone: those always cite the new number (SOMANYCERA's "Rs 58.80
cr" -> "Rs 75.80 cr in three entities"), so they keep a digit and still alert.
"""

from __future__ import annotations

import difflib
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.storage.models import Article

DEFAULT_WINDOW_HOURS = 3.0
DEFAULT_SIMILARITY_THRESHOLD = 0.65


def _normalize(text: str) -> str:
    return " ".join((text or "").lower().split())


def _has_digit(text: str) -> bool:
    return any(ch.isdigit() for ch in (text or ""))


def is_duplicate_of_recent_alert(
    session: Session,
    *,
    headline: str,
    direction: str,
    ticker: str | None = None,
    event_type: str | None = None,
    window_hours: float = DEFAULT_WINDOW_HOURS,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> bool:
    """True if a similar-enough alert (same direction, sent within the last
    window_hours) already went out. Compares the clean LLM headline, not the
    raw filing text — boilerplate like 'has informed the Exchange about...' is
    near-identical across unrelated filings and would cause false positives.

    Two independent suppression paths, either one triggering a duplicate:
      1. Text similarity >= threshold (cross-ticker / cross-exchange copies).
      2. Same ticker + same event_type as a recent sent alert, AND the candidate
         headline has no digit — a figure-free restatement ("Wins new order") of
         an event already alerted for that ticker. Only applies when both ticker
         and event_type are supplied; a headline WITH a number is never caught
         this way, so genuinely evolving updates (which cite the new figure) are
         preserved.

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

    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    stmt = select(Article.headline, Article.ticker, Article.event_type).where(
        Article.alert_sent == True,  # noqa: E712
        Article.direction == direction,
        Article.created_at >= cutoff,
    )
    norm_new = _normalize(headline)
    candidate_has_no_digit = not _has_digit(headline)
    for existing_headline, existing_ticker, existing_event_type in session.execute(stmt):
        if difflib.SequenceMatcher(None, norm_new, _normalize(existing_headline)).ratio() >= threshold:
            return True
        if (
            candidate_has_no_digit
            and ticker
            and event_type
            and existing_ticker == ticker
            and existing_event_type == event_type
        ):
            return True
    return False

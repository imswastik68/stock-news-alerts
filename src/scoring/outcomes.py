"""
Outcome tracking — the measurement half of the calibration loop.

For every alert that was sent, once each horizon (1/3/5 trading days) has matured
this records the stock's forward % return AND the NIFTY 50 (^NSEI) forward return
over the same window. Raw stock return alone can't tell "our news call added
value" apart from "the whole market moved" — evaluate.py computes
alpha = ret - idx_ret from the two, which is the number that actually answers
that question. Those returns are also what turn the hand-tuned confidence table
into an empirically calibrated one (see
src/scoring/confidence.py:BacktestedConfidenceProvider) and let evaluate.py
report real hit-rates.

Called from the pipeline each cycle with a small batch cap so it never dominates
a run. Fails soft: a ticker (or the index) Yahoo can't price is simply retried
next time, and given up on once it ages past the tracking window. The stock and
index fetches are independent — one succeeding while the other is still pending
is normal and gets backfilled on a later pass.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from src.ingestion import bse_bhavcopy
from src.scoring.market_data import (
    entry_basis_for,  # re-exported: the alerting layer needs it too
    get_forward_return,
    get_forward_return_from_open,
)
from src.storage.models import Article

logger = logging.getLogger(__name__)

# NIFTY 50. Fetched through the same get_forward_return()/close-cache path as
# any stock ticker, so within one process it's a single extra fetch, not one
# per article.
_BENCHMARK_TICKER = "^NSEI"

# (return column, index-return column, trading-days, min calendar age before
# the horizon can be mature)
_HORIZONS = [
    ("ret_1d", "idx_ret_1d", 1, 1),
    ("ret_3d", "idx_ret_3d", 3, 3),
    ("ret_5d", "idx_ret_5d", 5, 5),
]
# Stop retrying once an alert is older than this (dead/illiquid tickers Yahoo
# never prices shouldn't be re-fetched forever).
_MAX_TRACK_AGE_DAYS = 20


def _bhavcopy(session: Session, ticker: str, from_dt, tdays: int) -> float | None:
    try:
        return bse_bhavcopy.get_forward_return(session, ticker, from_dt, tdays)
    except Exception as exc:
        logger.debug("outcomes: bhavcopy fallback failed for %s: %s", ticker, exc)
        return None


def _stock_forward_return(
    session: Session, ticker: str, published_at, tdays: int, basis: str
) -> tuple[float | None, str]:
    """(forward return, the entry basis actually used) for one alerted ticker.

    Yahoo first, BSE's own bhavcopy as the fallback. yfinance prices nothing at
    all for a large share of BSE scrip codes — that gap alone made 63-71 alerted
    rows per horizon permanently unmeasurable and biased the whole track record
    toward the (larger, NSE-listed) names it could price. Yahoo stays first
    because it's already cached per process and covers every .NS ticker;
    bhavcopy only runs when Yahoo has nothing AND the ticker is a BSE scrip.

    The basis can come back different from the one asked for: bhavcopy stores
    EOD closes only, so a "next_open" request it has to serve falls back to the
    next session's CLOSE. That is still an honest post-news entry — the gap has
    already happened by then — just more conservative than the open, and the
    caller records which one was used so alpha is computed against a matching
    index leg."""
    if basis == "next_open":
        ret = get_forward_return_from_open(ticker, published_at, tdays)
        if ret is not None:
            return ret, "next_open"
        next_day = published_at.date() + timedelta(days=1)
        ret = _bhavcopy(session, ticker, next_day, tdays)
        return (ret, "next_close") if ret is not None else (None, basis)

    if basis == "next_close":
        # An earlier horizon already resolved this row to a next-session-close
        # entry. Every remaining horizon has to start from that same entry —
        # falling through to the close branch below would quietly re-measure
        # 3d and 5d from the pre-news close this basis exists to avoid.
        next_day = published_at.date() + timedelta(days=1)
        ret = get_forward_return(ticker, next_day, tdays)
        if ret is None:
            ret = _bhavcopy(session, ticker, next_day, tdays)
        return (ret, "next_close") if ret is not None else (None, basis)

    ret = get_forward_return(ticker, published_at, tdays)
    if ret is None:
        ret = _bhavcopy(session, ticker, published_at, tdays)
    return (ret, "close") if ret is not None else (None, basis)


def _index_forward_return(published_at, tdays: int, basis: str) -> float | None:
    """NIFTY 50 return over the same window AND the same entry basis as the
    stock leg. Mixing bases would silently corrupt alpha: a next-open stock
    entry measured against a prior-close index entry hands the index the
    overnight gap that the stock leg deliberately gave up."""
    if basis == "next_open":
        return get_forward_return_from_open(_BENCHMARK_TICKER, published_at, tdays)
    if basis == "next_close":
        return get_forward_return(_BENCHMARK_TICKER, published_at.date() + timedelta(days=1), tdays)
    return get_forward_return(_BENCHMARK_TICKER, published_at, tdays)


def _track(session: Session, limit: int, shadow: bool) -> int:
    now = datetime.now(timezone.utc)
    oldest = now - timedelta(days=_MAX_TRACK_AGE_DAYS)
    recorded = 0

    for col, idx_col, tdays, min_age_days in _HORIZONS:
        if recorded >= limit:
            break
        column = getattr(Article, col)
        idx_column = getattr(Article, idx_col)
        cutoff = now - timedelta(days=min_age_days)
        where = [
            Article.published_at <= cutoff,
            Article.published_at >= oldest,
            or_(column.is_(None), idx_column.is_(None)),
        ]
        if shadow:
            where += [
                Article.alert_sent == False,  # noqa: E712
                Article.direction != "neutral",
                Article.event_type != "classification_failed",
            ]
        else:
            where.append(Article.alert_sent == True)  # noqa: E712
        stmt = select(Article).where(and_(*where))
        if shadow:
            # Newest first. Unlike the alerted set, this one carries a long tail
            # of scrips neither Yahoo nor bhavcopy can ever price; in arbitrary
            # order those re-select every cycle and eat the whole budget, so
            # today's measurable rows would never be reached.
            stmt = stmt.order_by(Article.published_at.desc())
        for article in session.execute(stmt.limit(limit - recorded)).scalars():
            # A stored basis wins over a freshly derived one: the stock and index
            # legs are filled on independent passes, and the index must use
            # whatever the stock leg actually resolved to (bhavcopy can downgrade
            # next_open to next_close) or alpha subtracts mismatched windows.
            basis = article.entry_basis or entry_basis_for(article.published_at)

            if getattr(article, col) is None:
                ret, basis = _stock_forward_return(
                    session, article.ticker, article.published_at, tdays, basis
                )
                if ret is not None:
                    setattr(article, col, round(ret, 2))
                    article.entry_basis = basis
                    recorded += 1

            if getattr(article, idx_col) is None:
                idx_ret = _index_forward_return(article.published_at, tdays, basis)
                if idx_ret is not None:
                    setattr(article, idx_col, round(idx_ret, 2))
                    recorded += 1

            if recorded >= limit:
                break
        session.commit()

    return recorded


def track_outcomes(session: Session, limit: int = 60) -> int:
    """Fill in matured forward returns (stock + index) for alerted articles.
    Returns how many individual column values were recorded this call."""
    recorded = _track(session, limit, shadow=False)
    if recorded:
        logger.info("outcomes: recorded %d forward-return value(s)", recorded)
    return recorded


def track_shadow_outcomes(session: Session, limit: int = 40) -> int:
    """Same, for classified directional filings we chose NOT to alert on.

    This is the counterfactual track record, and without it several deferred
    decisions are simply unanswerable no matter how long we wait:

      - `credit_rating` is blocked in confidence_table.yaml, so it never sets
        alert_sent, so track_outcomes never measured it — and re-opening that
        retirement (ROADMAP P1.9) needs exactly the returns it was not
        collecting. 568 of 987 negative-catalyst filings sit in that bucket.
      - Rows that clear everything except `min_materiality_score` (ROADMAP
        P1.10) have the same problem: an auditor resignation scoring 0.60 is
        invisible forever, so there is no evidence on which to move the gate.

    Safe by construction: alert_sent stays False, and every consumer of these
    columns — get_hit_rate_stats() and evaluate.py — filters on
    alert_sent == True. So these rows feed no calibration and enter no reported
    hit-rate. They are measured and otherwise inert, which is the point: a
    decision to start alerting on them has to be made deliberately, on the
    evidence, not drift in because a column got populated.

    Runs after track_outcomes with its own smaller budget, so measuring the
    counterfactual can never starve the real track record.
    """
    recorded = _track(session, limit, shadow=True)
    if recorded:
        logger.info("outcomes: recorded %d shadow forward-return value(s)", recorded)
    return recorded

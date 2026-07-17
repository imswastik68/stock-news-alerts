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

from src.scoring.market_data import get_forward_return
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


def track_outcomes(session: Session, limit: int = 60) -> int:
    """Fill in matured forward returns (stock + index) for alerted articles.
    Returns how many individual column values were recorded this call."""
    now = datetime.now(timezone.utc)
    oldest = now - timedelta(days=_MAX_TRACK_AGE_DAYS)
    recorded = 0

    for col, idx_col, tdays, min_age_days in _HORIZONS:
        if recorded >= limit:
            break
        column = getattr(Article, col)
        idx_column = getattr(Article, idx_col)
        cutoff = now - timedelta(days=min_age_days)
        stmt = (
            select(Article)
            .where(
                and_(
                    Article.alert_sent == True,  # noqa: E712
                    Article.published_at <= cutoff,
                    Article.published_at >= oldest,
                    or_(column.is_(None), idx_column.is_(None)),
                )
            )
            .limit(limit - recorded)
        )
        for article in session.execute(stmt).scalars():
            if getattr(article, col) is None:
                ret = get_forward_return(article.ticker, article.published_at, tdays)
                if ret is not None:
                    setattr(article, col, round(ret, 2))
                    recorded += 1

            if getattr(article, idx_col) is None:
                idx_ret = get_forward_return(_BENCHMARK_TICKER, article.published_at, tdays)
                if idx_ret is not None:
                    setattr(article, idx_col, round(idx_ret, 2))
                    recorded += 1

            if recorded >= limit:
                break
        session.commit()

    if recorded:
        logger.info("outcomes: recorded %d forward-return value(s)", recorded)
    return recorded

"""
Outcome tracking — the measurement half of the calibration loop.

For every alert that was sent, once each horizon (1/3/5 trading days) has matured
this records the stock's forward % return. Those returns are what turn the
hand-tuned confidence table into an empirically calibrated one (see
src/scoring/confidence.py:BacktestedConfidenceProvider) and let evaluate.py report
real hit-rates.

Called from the pipeline each cycle with a small batch cap so it never dominates a
run. Fails soft: a ticker Yahoo can't price is simply retried next time, and given
up on once it ages past the tracking window.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from src.scoring.market_data import get_forward_return
from src.storage.models import Article

logger = logging.getLogger(__name__)

# (column, trading-days, min calendar age before the horizon can be mature)
_HORIZONS = [("ret_1d", 1, 1), ("ret_3d", 3, 3), ("ret_5d", 5, 5)]
# Stop retrying once an alert is older than this (dead/illiquid tickers Yahoo
# never prices shouldn't be re-fetched forever).
_MAX_TRACK_AGE_DAYS = 20


def track_outcomes(session: Session, limit: int = 25) -> int:
    """Fill in matured forward returns for alerted articles. Returns how many
    (article, horizon) values were recorded this call."""
    now = datetime.now(timezone.utc)
    oldest = now - timedelta(days=_MAX_TRACK_AGE_DAYS)
    recorded = 0

    for col, tdays, min_age_days in _HORIZONS:
        if recorded >= limit:
            break
        column = getattr(Article, col)
        cutoff = now - timedelta(days=min_age_days)
        stmt = (
            select(Article)
            .where(
                and_(
                    Article.alert_sent == True,  # noqa: E712
                    Article.published_at <= cutoff,
                    Article.published_at >= oldest,
                    column.is_(None),
                )
            )
            .limit(limit - recorded)
        )
        for article in session.execute(stmt).scalars():
            ret = get_forward_return(article.ticker, article.published_at, tdays)
            if ret is None:
                continue
            setattr(article, col, round(ret, 2))
            recorded += 1
            if recorded >= limit:
                break
        session.commit()

    if recorded:
        logger.info("outcomes: recorded %d forward-return value(s)", recorded)
    return recorded

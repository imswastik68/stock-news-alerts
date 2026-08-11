"""
"Already priced in" suppression — the one directional edge the data supports.

Measured on 371 real alerts with matured 3-day outcomes (production DB,
2026-08-11). Splitting BULLISH alerts by how far the stock had already moved in
the 5 trading sessions BEFORE the filing:

    pre-event drift      n     alpha-hit    avg alpha
    Q1 (fell most)       79       33%         -0.42%
    Q2                   77       35%         -1.35%
    Q3                   78       47%         +1.27%
    Q4 (ran up most)     77       21%         -2.41%

The top quartile is decisively the worst. Cut at +8%:

    drift <  +8%        266       36%         -0.32%
    drift >= +8%         45       22%         -3.09%

This is "buy the rumour, sell the news": by the time a good filing is public,
a stock that already ran up has priced it in, and the post-filing drift is
negative. It also explains the system's single worst bucket — earnings_surprise
with drift >= 8% is 18% hit / -4.02% alpha, and earnings is both the largest
category and the one we cannot judge properly without consensus estimates.

VALIDATED OUT OF SAMPLE, because a threshold picked and scored on the same data
proves nothing. Choosing +8% on the first half and testing on the later, unseen
half:

    first half  (in-sample)   keep 28% / drop 18%   avg alpha -0.72% vs -3.07%
    second half (OUT-of-sample) keep 43% / drop 29%  avg alpha +0.05% vs -3.12%

The suppressed group's average alpha is -3.07% then -3.12% — the effect is
stable, not an artifact of the fitting window (hit-rate difference p=0.035).

Deliberately BULLISH-ONLY. The symmetric bearish case (already fell hard before
bad news) has exactly ONE matured sample, so there is no evidence either way and
none is assumed.

Fails soft: no price data means no suppression. A filing we cannot price is not
evidence that it is priced in.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from src.ingestion import bse_bhavcopy
from src.scoring.market_data import get_prior_return

logger = logging.getLogger(__name__)

# Trading sessions of pre-filing drift to measure.
PRE_EVENT_LOOKBACK_DAYS = 5


def prior_move_pct(session: Session, ticker: str, published_at) -> float | None:
    """Pre-filing drift for a ticker: Yahoo first, BSE bhavcopy as the fallback
    for scrips Yahoo can't price (same two-source pattern outcome tracking
    uses). None when neither can price it."""
    move = get_prior_return(ticker, published_at, PRE_EVENT_LOOKBACK_DAYS)
    if move is not None:
        return move
    try:
        return bse_bhavcopy.get_prior_return(
            session, ticker, published_at, PRE_EVENT_LOOKBACK_DAYS
        )
    except Exception as exc:
        logger.debug("priced_in: bhavcopy prior-return failed for %s: %s", ticker, exc)
        return None


def is_priced_in(
    session: Session, *, ticker: str, direction: str, published_at, threshold_pct: float
) -> bool:
    """True if a BULLISH call should be suppressed because the stock has already
    run up `threshold_pct` or more over the pre-event window.

    threshold_pct <= 0 disables the check entirely."""
    if threshold_pct <= 0 or direction != "bullish":
        return False
    move = prior_move_pct(session, ticker, published_at)
    if move is None:
        return False  # unpriceable is not evidence of being priced in
    if move >= threshold_pct:
        logger.info(
            "priced_in: suppressing bullish alert for %s — already +%.1f%% over "
            "the prior %d sessions",
            ticker, move, PRE_EVENT_LOOKBACK_DAYS,
        )
        return True
    return False

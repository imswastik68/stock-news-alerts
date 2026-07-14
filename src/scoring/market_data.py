"""
Free market data via yfinance (Yahoo Finance) — used for two things:

  get_quote()          — recent close, 1-day % move, avg volume, for adding price
                         context to an alert (so you can tell a real mover /
                         liquid name from an illiquid micro-cap).
  get_forward_return() — the % move from an alert's date to N trading days later,
                         for the outcome-tracking / calibration loop.

Yahoo data for NSE (.NS) tickers is delayed (~15 min) and occasionally flaky, so
everything here fails soft: any problem returns None and the caller carries on.
Imports are lazy so the rest of the pipeline doesn't pay yfinance's import cost.
"""

from __future__ import annotations

import logging
import warnings
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

# Per-process cache of daily-close Series, keyed by ticker only — outcome
# tracking and quotes hit the same tickers repeatedly within one run.
#
# The window is a FIXED, generous size rather than the caller's requested
# `days` (bug found + fixed 2026-07-14): track_outcomes() calls this with a
# different `days` value per article depending on how old that article is, and
# the same ticker often gets multiple alerts. A per-ticker cache keyed without
# the window size meant a later call needing a LONGER lookback than an earlier
# cached call would silently reuse the too-short series — get_forward_return's
# `next(d >= from_date)` then matches the OLDEST available (wrong, too-recent)
# date as the base price instead of failing, producing a plausible-looking but
# WRONG forward return with no error. Confirmed by reproduction: a second call
# needing a 40-day window silently got the cached 13-day series and returned a
# fabricated number. Fetching one fixed window comfortably covering the whole
# valid tracking range removes the hazard rather than trying to cache-key it.
_CLOSES_LOOKBACK_DAYS = 60  # >> _MAX_TRACK_AGE_DAYS (20) + max horizon (5 trading days) + weekend/holiday padding
_close_cache: dict[str, object] = {}


def _closes(ticker: str):
    """Daily close Series for the last _CLOSES_LOOKBACK_DAYS calendar days, or None."""
    if ticker in _close_cache:
        return _close_cache[ticker]
    try:
        import yfinance as yf

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            hist = yf.Ticker(ticker).history(period=f"{_CLOSES_LOOKBACK_DAYS}d")
        closes = hist["Close"].dropna() if hist is not None and not hist.empty else None
    except Exception as exc:
        logger.debug("market_data: history failed for %s: %s", ticker, exc)
        closes = None
    _close_cache[ticker] = closes
    return closes


def get_quote(ticker: str) -> dict | None:
    """Recent close, 1-day % change, and 5-day average volume. None on failure.

    yfinance can return a NaN Close/Volume for the most-recent bar early in the
    trading session (the current-day row hasn't fully populated yet) — a plain
    `is None` check doesn't catch this (NaN is a valid float, not None), and
    NaN is also truthy in Python, so an unguarded `if vol:` would pass it
    through too. Confirmed live: this leaked literal "₹nan | ▼nan%" into two
    sent alerts. Every numeric field is NaN-checked before being returned."""
    try:
        import math

        import yfinance as yf

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            hist = yf.Ticker(ticker).history(period="10d")
        if hist is None or len(hist) < 2:
            return None
        close = float(hist["Close"].iloc[-1])
        if math.isnan(close):
            return None  # no usable price at all this call

        prev = float(hist["Close"].iloc[-2])
        pct_change = (close / prev - 1) * 100 if prev and not math.isnan(prev) else None
        if pct_change is not None and math.isnan(pct_change):
            pct_change = None

        avg_vol = float(hist["Volume"].tail(5).mean())
        if math.isnan(avg_vol):
            avg_vol = None

        return {
            "price": close,
            "pct_change": pct_change,
            "avg_volume": avg_vol,
        }
    except Exception as exc:
        logger.debug("market_data: quote failed for %s: %s", ticker, exc)
        return None


def get_forward_return(ticker: str, from_dt: datetime | date, trading_days: int) -> float | None:
    """% return from the first close on/after `from_dt` to `trading_days` trading
    bars later. None if the data isn't there yet (not matured), `from_dt` is
    older than the cached window can cover, or the fetch fails."""
    from_date = from_dt.date() if isinstance(from_dt, datetime) else from_dt
    closes = _closes(ticker)
    if closes is None or len(closes) == 0:
        return None

    # Index is tz-aware timestamps; compare on plain dates.
    dates = [ts.date() if hasattr(ts, "date") else ts for ts in closes.index]
    if dates[0] > from_date:
        # The fetched window doesn't reach back far enough to contain the true
        # base date. Matching anyway would silently pick the OLDEST available
        # (too-recent) date as "the" base price — exactly the bug that leaked a
        # fabricated forward-return with no error. Fail explicitly instead.
        return None
    base_idx = next((i for i, d in enumerate(dates) if d >= from_date), None)
    if base_idx is None:
        return None
    target_idx = base_idx + trading_days
    if target_idx >= len(closes):
        return None  # not enough trading days have elapsed yet

    try:
        base = float(closes.iloc[base_idx])
        target = float(closes.iloc[target_idx])
    except Exception:
        return None
    if base <= 0:
        return None
    return (target / base - 1) * 100

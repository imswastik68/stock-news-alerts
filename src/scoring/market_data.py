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

# Per-process cache of daily-close Series, keyed by ticker — outcome tracking and
# quotes hit the same tickers repeatedly within one run.
_close_cache: dict[str, object] = {}


def _closes(ticker: str, days: int = 30):
    """Daily close Series for the last `days` calendar days, or None."""
    if ticker in _close_cache:
        return _close_cache[ticker]
    try:
        import yfinance as yf

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            hist = yf.Ticker(ticker).history(period=f"{days}d")
        closes = hist["Close"].dropna() if hist is not None and not hist.empty else None
    except Exception as exc:
        logger.debug("market_data: history failed for %s: %s", ticker, exc)
        closes = None
    _close_cache[ticker] = closes
    return closes


def get_quote(ticker: str) -> dict | None:
    """Recent close, 1-day % change, and 5-day average volume. None on failure."""
    try:
        import yfinance as yf

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            hist = yf.Ticker(ticker).history(period="10d")
        if hist is None or len(hist) < 2:
            return None
        close = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2])
        avg_vol = float(hist["Volume"].tail(5).mean())
        return {
            "price": close,
            "pct_change": (close / prev - 1) * 100 if prev else None,
            "avg_volume": avg_vol,
        }
    except Exception as exc:
        logger.debug("market_data: quote failed for %s: %s", ticker, exc)
        return None


def get_forward_return(ticker: str, from_dt: datetime | date, trading_days: int) -> float | None:
    """% return from the first close on/after `from_dt` to `trading_days` trading
    bars later. None if the data isn't there yet (not matured) or fetch fails."""
    from_date = from_dt.date() if isinstance(from_dt, datetime) else from_dt
    # Need enough history to reach from_date and `trading_days` bars beyond it.
    span_days = (date.today() - from_date).days + 5
    closes = _closes(ticker, days=max(span_days, trading_days * 3 + 10))
    if closes is None or len(closes) == 0:
        return None

    # Index is tz-aware timestamps; compare on plain dates.
    dates = [ts.date() if hasattr(ts, "date") else ts for ts in closes.index]
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

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
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# NSE/BSE trade 09:15-15:30 IST. Exchange filings land at all hours and roughly
# a fifth arrive after the close, which is what makes the entry basis matter.
IST = timezone(timedelta(hours=5, minutes=30))
MARKET_CLOSE_IST_MINUTES = 15 * 60 + 30


def entry_basis_for(published_at: datetime) -> str:
    """"close" when the first close on/after publication printed with the news
    already public, otherwise "next_open".

    This decides whether a measured return is capturable at all. For a filing
    released after 15:30 IST, that day's close pre-dates the news, so measuring
    from it books the overnight gap as alpha — a move no order could have
    caught. Weekend filings are "close": the next close is Monday's, long after
    the news went public.

    Naive datetimes are read as UTC (what SQLite hands back), never as local
    time — guessing local would flip the basis on any non-UTC machine.
    """
    dt = published_at if published_at.tzinfo else published_at.replace(tzinfo=timezone.utc)
    ist = dt.astimezone(IST)
    if ist.weekday() >= 5:
        return "close"
    if ist.hour * 60 + ist.minute <= MARKET_CLOSE_IST_MINUTES:
        return "close"
    return "next_open"

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
# Opens live in their own dict, filled by the same single fetch as the closes.
# Kept separate rather than caching one frame so callers (and the tests that
# pre-seed _close_cache directly) that only ever want closes keep working.
_open_cache: dict[str, object] = {}


def _fetch_history(ticker: str) -> None:
    """One yfinance call; populates both the close and open caches for `ticker`."""
    closes = opens = None
    try:
        import yfinance as yf

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            hist = yf.Ticker(ticker).history(period=f"{_CLOSES_LOOKBACK_DAYS}d")
        if hist is not None and not hist.empty:
            closes = hist["Close"].dropna()
            # Open is treated as optional rather than assumed: a source without
            # it must still yield usable closes, not lose both to one KeyError.
            # Reindexed onto the close index so the two series share one bar
            # numbering. get_forward_return_from_open() takes its base price from
            # the opens and its exit price from the closes, so an independently
            # dropped NaN open bar would silently shift them apart and price an
            # entry against the wrong session's exit.
            if "Open" in hist:
                opens = hist["Open"].reindex(closes.index)
    except Exception as exc:
        logger.debug("market_data: history failed for %s: %s", ticker, exc)
    _close_cache[ticker] = closes
    _open_cache[ticker] = opens


def _closes(ticker: str):
    """Daily close Series for the last _CLOSES_LOOKBACK_DAYS calendar days, or None."""
    if ticker not in _close_cache:
        _fetch_history(ticker)
    return _close_cache[ticker]


def _opens(ticker: str):
    """Daily open Series sharing _closes(ticker)'s bar index, or None."""
    if ticker not in _open_cache:
        _fetch_history(ticker)
    return _open_cache.get(ticker)


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


def get_prior_return(ticker: str, before_dt: datetime | date, trading_days: int) -> float | None:
    """% move over the `trading_days` sessions immediately BEFORE `before_dt` —
    i.e. how far the stock had already run when the filing landed.

    This is the mirror of get_forward_return and exists for the priced-in check
    in src/scoring/priced_in.py. None when the cached window doesn't reach far
    enough back to cover the full pre-event span, rather than silently measuring
    a shorter one (the same failure that once fabricated forward returns)."""
    from_date = before_dt.date() if isinstance(before_dt, datetime) else before_dt
    closes = _closes(ticker)
    if closes is None or len(closes) == 0:
        return None

    dates = [ts.date() if hasattr(ts, "date") else ts for ts in closes.index]
    end_idx = next((i for i, d in enumerate(dates) if d >= from_date), None)
    if end_idx is None:
        return None
    start_idx = end_idx - trading_days
    if start_idx < 0:
        return None  # window doesn't cover the full pre-event span

    try:
        start = float(closes.iloc[start_idx])
        end = float(closes.iloc[end_idx])
    except Exception:
        return None
    if start <= 0:
        return None
    return (end / start - 1) * 100


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


def get_forward_return_from_open(
    ticker: str, from_dt: datetime | date, trading_days: int
) -> float | None:
    """% return from the OPEN of the first bar STRICTLY AFTER `from_dt` to the
    CLOSE `trading_days` bars of exposure later. None when the data isn't there
    yet, the window doesn't reach back far enough, or the fetch fails.

    This is the honest entry price for news published after the session closed.
    get_forward_return()'s base — the first close on/after the news date — is
    that day's close, which printed BEFORE the news existed, so the overnight
    gap reaction lands inside the measured return even though no order could
    ever have been filled at that base. Entering at the next open prices the gap
    out and leaves only what a trader could actually have captured.

    Measured on 271 delivered alerts (2026-09-17): the after-hours bucket scored
    +1.75% average 1d alpha on a close base against +0.26% for the genuinely
    tradable rows, i.e. most of the apparent edge was an artefact of this.

    Exposure is normalised to `trading_days` bars, matching get_forward_return
    (close of bar i -> close of bar i+N also spans N bars), so the two are
    directly comparable.
    """
    import math

    from_date = from_dt.date() if isinstance(from_dt, datetime) else from_dt
    closes = _closes(ticker)
    opens = _opens(ticker)
    if closes is None or opens is None or len(closes) == 0 or len(opens) != len(closes):
        return None

    dates = [ts.date() if hasattr(ts, "date") else ts for ts in closes.index]
    if dates[0] > from_date:
        # Same trap get_forward_return guards: the window may have cut off
        # trading days between the news and its first bar, so bar 0 is not
        # provably the next session. Fail instead of pricing the wrong one.
        return None
    base_idx = next((i for i, d in enumerate(dates) if d > from_date), None)
    if base_idx is None:
        return None
    target_idx = base_idx + trading_days - 1
    if target_idx >= len(closes):
        return None  # not enough trading days have elapsed yet

    try:
        base = float(opens.iloc[base_idx])
        target = float(closes.iloc[target_idx])
    except Exception:
        return None
    if base <= 0 or math.isnan(base) or math.isnan(target):
        return None
    return (target / base - 1) * 100

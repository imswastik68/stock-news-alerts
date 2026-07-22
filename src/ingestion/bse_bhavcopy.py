"""
BSE bhavcopy — official end-of-day prices for scrips Yahoo Finance can't price.

Why this exists: outcome tracking silently lost a third of its sample. evaluate.py
reported 63-71 alerted rows per horizon as "unpriceable" because yfinance returns
no data at all for many BSE scrip codes (543254.BO, 539016.BO, 544574.BO ...).
Those rows can never mature, and since BSE-only listings skew small-cap, the
measured slice wasn't representative of what was actually alerted.

BSE publishes the fix itself, free, no key and no auth:

    https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_<YYYYMMDD>_F_0000.CSV

~4850 rows per trading day covering every BSE equity including SME. Verified live
2026-07-22: all ten scrips evaluate.py called unpriceable are present with real
closes (AWHCL 446.35, TATACAP 349.80, NEIL 6.42, PORWAL 53.92, RIKHAV 47.00 ...).

TWO TRAPS, both confirmed live and both guarded below:

1. A non-trading day (weekend/holiday) — and today before EOD publication —
   returns **HTTP 200 with a 12,565-byte HTML page**, NOT a 404. Status code is
   therefore worthless for "did this day trade"; the content must be validated.
   The older `EQ<ddmmyy>_CSV.ZIP` endpoint fails the same way (constant 12,565-byte
   error page for every date) which is why it isn't used at all.
2. The scrip code is `FinInstrmId` (column 6), NOT column 1 (that's TradDt) and
   NOT `TckrSymb`. Parsing by position rather than header name silently yields
   zero matches.

Each trading day is fetched at most ONCE ever: the parsed closes and a
"was this a trading day" marker are persisted (src/storage/models.py), and that
DB is what the GitHub Actions cache already carries between runs. Steady state
is roughly one fetch per day, not one per pipeline cycle. Days older than the
tracking window are pruned so the cached DB stays small.

Fails soft per the project contract: any network/parse problem returns None and
the caller falls back to yfinance.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
from datetime import date, datetime, timedelta

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import ROOT_DIR
from src.storage.models import BseBhavcopyDay, BseClose

logger = logging.getLogger(__name__)

_URL = "https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{d}_F_0000.CSV"
_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
_TIMEOUT = 30

# A real bhavcopy starts with this header. The HTML error page served for
# non-trading days does not, which is the only reliable way to tell them apart
# (see trap 1 in the module docstring).
_EXPECTED_HEADER_FIELD = "TradDt"
_SCRIP_COL = "FinInstrmId"
_CLOSE_COL = "ClsPric"
_ISIN_COL = "ISIN"

# How far forward to look for trading days when maturing a horizon: 5 trading
# days plus weekends and a holiday run comfortably fit in 15 calendar days.
_FORWARD_CALENDAR_DAYS = 15
# Keep enough history to cover the whole tracking window; prune beyond it so the
# cached DB doesn't grow without bound.
_RETAIN_DAYS = 40


def _parse(text: str) -> dict[str, tuple[float, str]] | None:
    """{scrip_code: (close, isin)}, or None if this isn't a real bhavcopy."""
    if not text or _EXPECTED_HEADER_FIELD not in text[:200]:
        return None
    try:
        reader = csv.DictReader(io.StringIO(text))
        out: dict[str, tuple[float, str]] = {}
        for row in reader:
            scrip = (row.get(_SCRIP_COL) or "").strip()
            close_raw = (row.get(_CLOSE_COL) or "").strip()
            if not scrip or not close_raw:
                continue
            try:
                close = float(close_raw)
            except ValueError:
                continue
            if close <= 0:
                continue
            out[scrip] = (close, (row.get(_ISIN_COL) or "").strip())
        return out or None
    except Exception as exc:
        logger.warning("bse_bhavcopy: parse failed: %s", exc)
        return None


def fetch_bhavcopy(d: date) -> dict[str, tuple[float, str]] | None:
    """Download and parse one day's bhavcopy. None = not a trading day (or the
    fetch/parse failed) — callers must not treat that as 'no such scrip'."""
    url = _URL.format(d=d.strftime("%Y%m%d"))
    try:
        resp = requests.get(url, headers=_UA, timeout=_TIMEOUT)
        if resp.status_code != 200:
            return None
        return _parse(resp.text)
    except Exception as exc:
        logger.warning("bse_bhavcopy: fetch failed for %s: %s", d, exc)
        return None


def ensure_day_cached(session: Session, d: date) -> bool:
    """Fetch+persist one day's closes if not already known. Returns True if `d`
    was a trading day. Never re-fetches a day already recorded (either way)."""
    marker = session.get(BseBhavcopyDay, d)
    if marker is not None:
        return marker.is_trading_day

    parsed = fetch_bhavcopy(d)
    if parsed is None:
        # Record the negative too, so a weekend isn't re-fetched every cycle.
        # Today's date is deliberately NOT recorded: EOD data appears later the
        # same day, so caching "not a trading day" now would poison it.
        if d < datetime.now().date():
            session.add(BseBhavcopyDay(trade_date=d, is_trading_day=False))
            session.commit()
        return False

    session.add(BseBhavcopyDay(trade_date=d, is_trading_day=True))
    session.add_all(
        BseClose(trade_date=d, scrip=scrip, close=close)
        for scrip, (close, _isin) in parsed.items()
    )
    session.commit()
    logger.info("bse_bhavcopy: cached %d closes for %s", len(parsed), d)
    return True


def _scrip_of(ticker: str) -> str | None:
    """'543254.BO' -> '543254'. None for anything that isn't a BSE ticker."""
    if not ticker or not ticker.endswith(".BO"):
        return None
    scrip = ticker[:-3].strip()
    return scrip or None


def get_forward_return(
    session: Session, ticker: str, from_dt: datetime | date, trading_days: int
) -> float | None:
    """% move for a BSE scrip from the first traded close on/after `from_dt` to
    `trading_days` sessions later, using BSE's own EOD data. None if the horizon
    hasn't matured, the scrip doesn't trade, or the data isn't available."""
    scrip = _scrip_of(ticker)
    if scrip is None:
        return None
    from_date = from_dt.date() if isinstance(from_dt, datetime) else from_dt

    today = datetime.now().date()
    for offset in range(_FORWARD_CALENDAR_DAYS + 1):
        d = from_date + timedelta(days=offset)
        if d > today:
            break
        ensure_day_cached(session, d)

    stmt = (
        select(BseClose.trade_date, BseClose.close)
        .where(
            BseClose.scrip == scrip,
            BseClose.trade_date >= from_date,
            BseClose.trade_date <= from_date + timedelta(days=_FORWARD_CALENDAR_DAYS),
        )
        .order_by(BseClose.trade_date)
    )
    rows = list(session.execute(stmt))
    if len(rows) <= trading_days:
        return None  # horizon hasn't matured for this scrip yet

    base = rows[0][1]
    target = rows[trading_days][1]
    if base <= 0:
        return None
    return (target / base - 1) * 100


def prune_old_days(session: Session, retain_days: int = _RETAIN_DAYS) -> int:
    """Drop cached closes older than the tracking window. Returns rows deleted."""
    cutoff = datetime.now().date() - timedelta(days=retain_days)
    deleted = (
        session.query(BseClose).filter(BseClose.trade_date < cutoff).delete(synchronize_session=False)
    )
    session.query(BseBhavcopyDay).filter(BseBhavcopyDay.trade_date < cutoff).delete(
        synchronize_session=False
    )
    session.commit()
    return int(deleted or 0)


def build_isin_map(d: date) -> dict[str, str]:
    """{ISIN: scrip_code} from one day's bhavcopy — the input to deterministic
    BSE->NSE resolution (see src/ingestion/symbol_master.py)."""
    parsed = fetch_bhavcopy(d)
    if not parsed:
        return {}
    return {isin: scrip for scrip, (_close, isin) in parsed.items() if isin}


# {scrip_code: ISIN} from the most recent available bhavcopy, memoized in-process
# and on disk. This is what lets a BSE filing be resolved to its NSE ticker
# EXACTLY (scrip -> ISIN -> NSE symbol) instead of by fuzzy company-name match.
_scrip_to_isin: dict[str, str] | None = None
_SCRIP_ISIN_CACHE = ROOT_DIR / ".bse_scrip_isin.json"
_SCRIP_ISIN_TTL_SECONDS = 24 * 60 * 60


def get_scrip_to_isin() -> dict[str, str]:
    """{scrip_code: ISIN}, from the latest trading day with a published bhavcopy.
    Walks back a few days so a weekend/holiday (or today's not-yet-published
    file) doesn't leave it empty. Fails soft to {}."""
    global _scrip_to_isin
    if _scrip_to_isin is not None:
        return _scrip_to_isin

    if _SCRIP_ISIN_CACHE.exists():
        try:
            if time.time() - _SCRIP_ISIN_CACHE.stat().st_mtime <= _SCRIP_ISIN_TTL_SECONDS:
                _scrip_to_isin = json.loads(_SCRIP_ISIN_CACHE.read_text())
                return _scrip_to_isin
        except Exception as exc:
            logger.debug("bse_bhavcopy: scrip/ISIN cache read failed: %s", exc)

    today = datetime.now().date()
    for back in range(1, 8):
        parsed = fetch_bhavcopy(today - timedelta(days=back))
        if parsed:
            mapping = {scrip: isin for scrip, (_c, isin) in parsed.items() if isin}
            try:
                _SCRIP_ISIN_CACHE.write_text(json.dumps(mapping))
            except Exception as exc:
                logger.debug("bse_bhavcopy: scrip/ISIN cache write failed: %s", exc)
            _scrip_to_isin = mapping
            return _scrip_to_isin

    _scrip_to_isin = {}
    return _scrip_to_isin

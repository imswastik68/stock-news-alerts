"""
NSE corporate announcement fetcher — the high-signal backbone.

This is the same feed the pro platforms (Dhan/ScanX/Groww) surface as "News
Flash": every company's exchange filing, minute-fresh, category-tagged. Two
modes:

  fetch_nse_market_wide()   — the rolling market-wide feed (latest ~20 filings
                              across ALL NSE stocks). Polled every couple of
                              minutes this is a near-real-time stream of every
                              material disclosure in the market.
  fetch_nse_announcements() — per-symbol history for a specific watchlist.

Each RawArticle carries the exchange `category` (NSE `desc`), which the pipeline
uses to keep only high-impact filings and drop procedural ones.

Fails soft: any network/shape problem logs and returns [] — the pipeline keeps
running even if NSE blocks or changes this unofficial endpoint.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import requests

from src.ingestion.common import RawArticle

logger = logging.getLogger(__name__)

_NSE_BASE = "https://www.nseindia.com"
_NSE_API = _NSE_BASE + "/api/corporate-announcements"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.nseindia.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

IST = timezone(timedelta(hours=5, minutes=30))
_SLEEP_BETWEEN_SYMBOLS_SECONDS = 0.5


def _parse_nse_dt(dt_str: str) -> datetime | None:
    """Parse '02-Jun-2026 23:49:32' → aware datetime (IST)."""
    try:
        dt = datetime.strptime(dt_str.strip(), "%d-%b-%Y %H:%M:%S")
        return dt.replace(tzinfo=IST)
    except ValueError:
        return None


def _symbol_for_ticker(ticker: str) -> str:
    """RELIANCE.NS -> RELIANCE"""
    return ticker.split(".")[0].upper()


def _new_session() -> requests.Session | None:
    """NSE's API rejects cold requests; a prior GET to the homepage sets the
    cookies the API needs. Returns None if even the warm-up fails."""
    session = requests.Session()
    session.headers.update(_HEADERS)
    try:
        session.get(_NSE_BASE + "/", timeout=15)
        return session
    except Exception as exc:
        logger.warning("nse_announcements: session warm-up failed: %s", exc)
        return None


def _item_to_article(item: dict, cutoff: datetime) -> RawArticle | None:
    symbol = str(item.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    filed_at = _parse_nse_dt(str(item.get("an_dt") or "").strip())
    if filed_at is None or filed_at < cutoff:
        return None

    category = str(item.get("desc") or "").strip()
    headline = str(item.get("attchmntText") or category or "").strip()[:300]
    if not headline:
        return None

    attachment_url = str(item.get("attchmntFile") or "")
    seq = str(item.get("seq_id") or "")
    return RawArticle(
        ticker=f"{symbol}.NS",
        headline=headline,
        summary=category,
        url=attachment_url or f"nse://{symbol}/{seq or item.get('an_dt')}",
        source="nse_announcements",
        published_at=filed_at.astimezone(timezone.utc),
        category=category,
    )


def fetch_nse_market_wide(hours_back: int = 12) -> list[RawArticle]:
    """Rolling market-wide feed: the latest filings across ALL NSE stocks.
    This is the backbone of the high-signal stream."""
    session = _new_session()
    if session is None:
        return []

    try:
        resp = session.get(_NSE_API, params={"index": "equities"}, timeout=15)
        resp.raise_for_status()
        items = resp.json()
    except Exception as exc:
        logger.warning("nse_announcements: market-wide fetch failed: %s", exc)
        return []

    if not isinstance(items, list):
        logger.warning("nse_announcements: unexpected market-wide response type")
        return []

    cutoff = datetime.now(IST) - timedelta(hours=hours_back)
    articles = [a for a in (_item_to_article(it, cutoff) for it in items) if a is not None]
    logger.info("nse_announcements: %d market-wide filing(s) in last %dh", len(articles), hours_back)
    return articles


def fetch_nse_announcements(
    watchlist_tickers: list[str], hours_back: int = 48
) -> list[RawArticle]:
    """Per-symbol history for a specific watchlist (used only in watchlist mode)."""
    session = _new_session()
    if session is None:
        return []

    cutoff = datetime.now(IST) - timedelta(hours=hours_back)
    articles: list[RawArticle] = []

    for i, ticker in enumerate(watchlist_tickers):
        symbol = _symbol_for_ticker(ticker)
        try:
            resp = session.get(
                _NSE_API, params={"index": "equities", "symbol": symbol}, timeout=15
            )
            resp.raise_for_status()
            items = resp.json()
        except Exception as exc:
            logger.warning("nse_announcements: fetch failed for %s: %s", symbol, exc)
            items = []

        if isinstance(items, list):
            for item in items:
                article = _item_to_article(item, cutoff)
                if article is not None:
                    articles.append(article)

        if i < len(watchlist_tickers) - 1:
            time.sleep(_SLEEP_BETWEEN_SYMBOLS_SECONDS)

    logger.info(
        "nse_announcements: %d filing(s) in last %dh for %d watchlist ticker(s)",
        len(articles),
        hours_back,
        len(watchlist_tickers),
    )
    return articles

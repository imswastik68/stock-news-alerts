"""
Indian business-news RSS ingestion (Economic Times + Moneycontrol).

These are market-wide feeds (not per-stock), so each item is matched against the
watchlist by company name and only kept if a name matches. Fresh and free, they
broaden coverage beyond Google News. Fails soft per the project contract: a bad
feed is logged and skipped, never crashes the pipeline.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

from src.config import WatchlistEntry
from src.ingestion.common import RawArticle

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_TIMEOUT_SECONDS = 15

# (source_label, feed_url). Market-wide feeds — items are name-filtered below.
_FEEDS = [
    ("moneycontrol", "https://www.moneycontrol.com/rss/latestnews.xml"),
    ("moneycontrol", "https://www.moneycontrol.com/rss/results.xml"),
    ("moneycontrol", "https://www.moneycontrol.com/rss/marketreports.xml"),
    ("economic_times", "https://economictimes.indiatimes.com/rssfeeds/1977021501.cms"),
]


def _parse_pub_date(pub_date: str | None) -> datetime | None:
    if not pub_date:
        return None
    try:
        dt = parsedate_to_datetime(pub_date)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _match_ticker(text: str, watchlist: list[WatchlistEntry]) -> str | None:
    """Return the ticker whose company name (or its first significant word)
    appears in the article text, else None."""
    lowered = text.lower()
    for entry in watchlist:
        name = entry.name.lower()
        if name in lowered:
            return entry.ticker
        # Also try the distinctive first word (e.g. "Reliance", "Infosys") so
        # "Reliance Q1 results" matches the "Reliance Industries" watchlist name.
        first_word = name.split()[0]
        if len(first_word) >= 4 and first_word in lowered:
            return entry.ticker
    return None


def _fetch_feed(source_label: str, url: str, watchlist: list[WatchlistEntry]) -> list[RawArticle]:
    try:
        resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT_SECONDS)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as exc:
        logger.warning("indian_rss: fetch/parse failed for %s: %s", url, exc)
        return []

    articles: list[RawArticle] = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        description = (item.findtext("description") or "").strip()
        pub_date = _parse_pub_date(item.findtext("pubDate"))
        if not title or not link or pub_date is None:
            continue

        ticker = _match_ticker(f"{title} {description}", watchlist)
        if ticker is None:
            continue

        articles.append(
            RawArticle(
                ticker=ticker,
                headline=title,
                summary=description,
                url=link,
                source=source_label,
                published_at=pub_date,
            )
        )
    return articles


def fetch_indian_rss(watchlist: list[WatchlistEntry]) -> list[RawArticle]:
    if not watchlist:
        return []

    articles: list[RawArticle] = []
    for source_label, url in _FEEDS:
        articles.extend(_fetch_feed(source_label, url, watchlist))

    logger.info(
        "indian_rss: %d watchlist-matched article(s) across %d feed(s)",
        len(articles),
        len(_FEEDS),
    )
    return articles

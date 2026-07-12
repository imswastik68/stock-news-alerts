"""
Google News RSS ingestion — free, no API key, no hard rate limit.

Queries `https://news.google.com/rss/search` per watchlist ticker. Parsed with
the stdlib xml.etree (no extra dependency). Polite pacing (sleep between
tickers) since this is an unofficial-but-public endpoint; fails soft (returns
[] for that ticker) on any error so one broken feed never stops the pipeline.
"""

from __future__ import annotations

import logging
import math
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests

from src.config import get_settings
from src.ingestion.common import RawArticle

logger = logging.getLogger(__name__)

_RSS_URL = "https://news.google.com/rss/search"
_SLEEP_BETWEEN_TICKERS_SECONDS = 1.0
_TIMEOUT_SECONDS = 15
# Google's search results default to relevance order, which surfaces weeks-old
# articles for large-caps. The `when:Nd` operator restricts to the last N days —
# the single biggest lever for alert freshness. Capped so we never ask for a
# huge window even if MAX_NEWS_AGE_HOURS is set high.
_MAX_RECENCY_DAYS = 7


def _recency_days(max_news_age_hours: int) -> int:
    if max_news_age_hours <= 0:
        return _MAX_RECENCY_DAYS
    return max(1, min(_MAX_RECENCY_DAYS, math.ceil(max_news_age_hours / 24)))


def _build_url(company_name: str, recency_days: int) -> str:
    query = quote(f'"{company_name}" when:{recency_days}d')
    return f"{_RSS_URL}?q={query}&hl=en-IN&gl=IN&ceid=IN:en"


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


def _fetch_one(ticker: str, company_name: str, recency_days: int, max_items: int) -> list[RawArticle]:
    url = _build_url(company_name, recency_days)
    try:
        resp = requests.get(url, timeout=_TIMEOUT_SECONDS)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as exc:
        logger.warning("google_news: fetch failed for %s: %s", ticker, exc)
        return []

    articles: list[RawArticle] = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = _parse_pub_date(item.findtext("pubDate"))
        description = (item.findtext("description") or "").strip()
        if not title or not link or pub_date is None:
            continue
        articles.append(
            RawArticle(
                ticker=ticker,
                headline=title,
                summary=description,
                url=link,
                source="google_news",
                published_at=pub_date,
            )
        )
    # Keep the freshest max_items, not the first max_items Google happened to
    # return — the feed isn't strictly date-ordered even with the when: filter.
    articles.sort(key=lambda a: a.published_at, reverse=True)
    return articles[:max_items]


def fetch_google_news(watchlist: list[tuple[str, str]]) -> list[RawArticle]:
    """watchlist: list of (ticker, company_name) tuples."""
    settings = get_settings()
    recency_days = _recency_days(settings.max_news_age_hours)
    max_items = settings.max_google_news_per_ticker
    all_articles: list[RawArticle] = []
    for i, (ticker, company_name) in enumerate(watchlist):
        all_articles.extend(_fetch_one(ticker, company_name, recency_days, max_items))
        if i < len(watchlist) - 1:
            time.sleep(_SLEEP_BETWEEN_TICKERS_SECONDS)

    logger.info(
        "google_news: %d article(s) across %d ticker(s)",
        len(all_articles),
        len(watchlist),
    )
    return all_articles

"""
NewsAPI ingestion — free dev tier: 100 requests/day, 1-month lookback limit.

Used sparingly as a broader-media fallback alongside NSE announcements and
Google News RSS. A daily request counter is persisted in the DB (ApiUsage) so
the pipeline never exceeds NEWSAPI_DAILY_CAP, and each ticker is queried at
most once per hour (TickerFetchLog) so the daily budget isn't burned by the
first few poll cycles of the day.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests
from sqlalchemy.orm import Session

from src.config import get_settings
from src.ingestion.common import RawArticle
from src.storage.db import (
    get_api_usage_today,
    get_last_ticker_fetch,
    increment_api_usage,
    set_last_ticker_fetch,
)

logger = logging.getLogger(__name__)

_NEWSAPI_URL = "https://newsapi.org/v2/everything"
_SOURCE_NAME = "newsapi"
_MIN_TICKER_INTERVAL = timedelta(hours=1)
_LOOKBACK_HOURS = 48
_TIMEOUT_SECONDS = 15


def _parse_published_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _should_query_ticker(session: Session, ticker: str) -> bool:
    last_fetch = get_last_ticker_fetch(session, ticker, _SOURCE_NAME)
    if last_fetch is None:
        return True
    if last_fetch.tzinfo is None:
        last_fetch = last_fetch.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last_fetch >= _MIN_TICKER_INTERVAL


def fetch_newsapi(session: Session, ticker: str, company_name: str) -> list[RawArticle]:
    """Fetch recent articles for one ticker from NewsAPI, respecting the daily
    budget and per-ticker hourly interval. Returns [] if budget exhausted, the
    ticker was queried too recently, the key is missing, or the request fails."""
    settings = get_settings()
    if not settings.newsapi_api_key:
        return []

    usage_today = get_api_usage_today(session, _SOURCE_NAME)
    if usage_today >= settings.newsapi_daily_cap:
        logger.warning(
            "newsapi: daily cap (%d) reached, skipping %s",
            settings.newsapi_daily_cap,
            ticker,
        )
        return []

    if not _should_query_ticker(session, ticker):
        return []

    from_time = (datetime.now(timezone.utc) - timedelta(hours=_LOOKBACK_HOURS)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    params = {
        "q": company_name,
        "from": from_time,
        "sortBy": "publishedAt",
        "language": "en",
        "apiKey": settings.newsapi_api_key,
    }

    try:
        resp = requests.get(_NEWSAPI_URL, params=params, timeout=_TIMEOUT_SECONDS)
        increment_api_usage(session, _SOURCE_NAME)
        set_last_ticker_fetch(session, ticker, _SOURCE_NAME)

        if resp.status_code == 429:
            logger.warning("newsapi: rate limited (429) for %s", ticker)
            return []
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("newsapi: fetch failed for %s: %s", ticker, exc)
        return []

    articles: list[RawArticle] = []
    for item in data.get("articles", []):
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        published_at = _parse_published_at(item.get("publishedAt"))
        if not title or not url or published_at is None:
            continue
        articles.append(
            RawArticle(
                ticker=ticker,
                headline=title,
                summary=(item.get("description") or "").strip(),
                url=url,
                source=_SOURCE_NAME,
                published_at=published_at,
            )
        )
    return articles

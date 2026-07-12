"""
Official exchange RSS feeds — the free real-time path to BOTH exchanges.

  BSE: https://www.bseindia.com/data/xml/announcements.xml
       BSE's JSON API blocks scripted access, but this official RSS does not —
       it is the only free market-wide BSE announcements source. Items carry the
       scrip code and a direct PDF link.
  NSE: https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml
       Supplements the date-range API with the very freshest filings (the RSS
       updates the moment a filing lands; the API poll then backfills depth).

Both are rolling windows of recent items only — the 2-minute pipeline polling is
what makes them a continuous stream; historical depth lives in our own DB.
Fails soft per the project contract: any fetch/parse problem returns [].
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

from src.ingestion.common import RawArticle

logger = logging.getLogger(__name__)

_BSE_RSS = "https://www.bseindia.com/data/xml/announcements.xml"
_NSE_RSS = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

IST = timezone(timedelta(hours=5, minutes=30))
_TIMEOUT = 15


def _parse_dt(value: str | None) -> datetime | None:
    """Both feeds use '13-Jul-2026 00:06:50' (IST); BSE notices sometimes use
    RFC-822 — try both."""
    if not value:
        return None
    value = value.strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%a, %d %b %Y %H:%M:%S"):
        try:
            return datetime.strptime(value[:31], fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def _fetch_items(url: str) -> list:
    resp = requests.get(url, headers=_UA, timeout=_TIMEOUT)
    resp.raise_for_status()
    return ET.fromstring(resp.content).findall(".//item")


def fetch_bse_rss(hours_back: int = 36) -> list[RawArticle]:
    """Market-wide BSE announcements. Ticker is '<scripcode>.BO' (yfinance
    accepts numeric .BO codes, so price context still works)."""
    try:
        items = _fetch_items(_BSE_RSS)
    except Exception as exc:
        logger.warning("exchange_rss: BSE fetch failed: %s", exc)
        return []

    cutoff = datetime.now(IST) - timedelta(hours=hours_back)
    articles: list[RawArticle] = []
    for it in items:
        pub = _parse_dt(it.findtext("pubDate"))
        link = (it.findtext("link") or "").strip()
        desc = (it.findtext("description") or "").strip()
        scrip = (it.findtext("scripcode") or "").strip()
        title = (it.findtext("title") or "").strip()
        if pub is None or pub < cutoff or not link or not (desc or title):
            continue
        if not scrip:
            m = re.search(r"\((\d{6})\)", title)
            scrip = m.group(1) if m else ""
        company = re.sub(r"\s*\(\d{6}\)\s*$", "", title).strip()
        articles.append(
            RawArticle(
                ticker=f"{scrip}.BO" if scrip else (company or "BSE?"),
                headline=(desc or title)[:300],
                summary=company,
                url=link,
                source="bse_rss",
                published_at=pub.astimezone(timezone.utc),
                # No structured category in the RSS; the description text is
                # keyword-rich enough for the impact filter (e.g. "buyback",
                # "trading window") — unknown text defaults to MEDIUM, so the
                # PDF still gets read and the LLM judges materiality.
                category=(desc or title)[:120],
                attachment_url=link if link.lower().endswith(".pdf") else "",
            )
        )

    logger.info("exchange_rss: %d BSE filing(s) from RSS", len(articles))
    return articles


def fetch_nse_rss(hours_back: int = 36) -> list[RawArticle]:
    """Freshest NSE filings from the official RSS. The ticker is best-effort
    (derived from the PDF filename prefix); the date-range API remains the
    authoritative NSE source — pipeline dedup by URL keeps them from clashing."""
    try:
        items = _fetch_items(_NSE_RSS)
    except Exception as exc:
        logger.warning("exchange_rss: NSE fetch failed: %s", exc)
        return []

    cutoff = datetime.now(IST) - timedelta(hours=hours_back)
    articles: list[RawArticle] = []
    for it in items:
        pub = _parse_dt(it.findtext("pubDate"))
        link = (it.findtext("link") or "").strip()
        desc = (it.findtext("description") or "").strip()
        title = (it.findtext("title") or "").strip()
        if pub is None or pub < cutoff or not link:
            continue

        # description looks like "...has informed the Exchange about X |SUBJECT: <category>"
        category = ""
        m = re.search(r"SUBJECT:\s*(.+)$", desc)
        if m:
            category = m.group(1).strip()[:120]

        # PDF filename prefix approximates the symbol (e.g. AWHCLP_... ~ AWHCL);
        # imperfect, but API-fetched duplicates (proper symbol) win via dedup.
        fname = link.rsplit("/", 1)[-1]
        prefix = fname.split("_", 1)[0].upper()
        ticker = f"{prefix}.NS" if prefix.isalpha() and 1 < len(prefix) <= 12 else (title or "NSE?")

        articles.append(
            RawArticle(
                ticker=ticker,
                headline=(desc.split("|SUBJECT:")[0].strip() or title)[:300],
                summary=title,
                url=link,
                source="nse_announcements",  # same source class/quality as the API
                published_at=pub.astimezone(timezone.utc),
                category=category or "General Updates",
                attachment_url=link if link.lower().endswith(".pdf") else "",
            )
        )

    logger.info("exchange_rss: %d NSE filing(s) from RSS", len(articles))
    return articles

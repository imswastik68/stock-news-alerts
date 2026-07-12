"""
BSE corporate announcement fetcher.

BSE uses numeric scrip codes, so entries in watchlist.yaml can optionally add
`bse_code`. Entries without a BSE code are skipped. The endpoint is unofficial
from an API-stability perspective, so this fetcher follows the project's
fail-soft contract and returns [] on any network or shape problem.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests

from src.config import WatchlistEntry
from src.ingestion.common import RawArticle

logger = logging.getLogger(__name__)

_BSE_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"
_ATTACHMENT_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bseindia.com/",
    "Accept": "application/json, text/plain, */*",
}

IST = timezone(timedelta(hours=5, minutes=30))


def _parse_bse_dt(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%d %b %Y %H:%M:%S", "%d-%m-%Y %H:%M:%S"):
        try:
            return datetime.strptime(value.strip()[:19], fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def _attachment_url(item: dict, scrip_code: str, published_at: datetime) -> str:
    attachment = str(item.get("ATTACHMENTNAME") or "").strip()
    if attachment:
        return f"{_ATTACHMENT_BASE}/{attachment}"
    news_id = str(item.get("NEWSID") or "").strip()
    return f"bse://{scrip_code}/{news_id or published_at.isoformat()}"


def _extract_rows(data) -> list[dict]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("Table", "Table1", "data", "Data"):
        rows = data.get(key)
        if isinstance(rows, list):
            return rows
    return []


def fetch_bse_announcements(watchlist: list[WatchlistEntry], hours_back: int = 20) -> list[RawArticle]:
    code_to_entry = {entry.bse_code: entry for entry in watchlist if entry.bse_code}
    if not code_to_entry:
        return []

    now_ist = datetime.now(IST)
    from_date = (now_ist - timedelta(hours=hours_back)).strftime("%Y%m%d")
    to_date = now_ist.strftime("%Y%m%d")
    articles: list[RawArticle] = []

    session = requests.Session()
    session.headers.update(_HEADERS)
    try:
        session.get("https://www.bseindia.com/", timeout=15)
    except Exception:
        pass  # cookie warm-up is best-effort

    cutoff = now_ist - timedelta(hours=hours_back)
    for scrip_code, entry in code_to_entry.items():
        params = {
            "strCat": "-1",
            "strPrevDate": from_date,
            "strScrip": scrip_code,
            "strSearch": "P",
            "strToDate": to_date,
            "strType": "C",
        }
        try:
            resp = session.get(_BSE_URL, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("bse_announcements: fetch failed for %s: %s", entry.ticker, exc)
            continue

        rows = _extract_rows(data)
        if not rows:
            continue

        for item in rows:
            published_at = _parse_bse_dt(str(item.get("DT_TM") or ""))
            if published_at is None or published_at < cutoff:
                continue

            headline = str(item.get("NEWSSUB") or item.get("HEADLINE") or "").strip()[:300]
            if not headline:
                continue

            category = str(item.get("CATEGORYNAME") or item.get("SUBCATNAME") or "").strip()
            attach = _attachment_url(item, scrip_code, published_at)
            has_pdf = str(item.get("ATTACHMENTNAME") or "").strip() != ""
            articles.append(
                RawArticle(
                    ticker=entry.ticker,
                    headline=headline,
                    summary=str(item.get("MORE") or category).strip(),
                    url=attach,
                    source="bse_announcements",
                    published_at=published_at.astimezone(timezone.utc),
                    category=category,
                    attachment_url=attach if has_pdf else "",
                )
            )

    logger.info(
        "bse_announcements: %d filing(s) in last %dh for %d configured BSE code(s)",
        len(articles),
        hours_back,
        len(code_to_entry),
    )
    return articles

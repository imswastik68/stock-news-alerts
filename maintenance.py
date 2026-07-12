"""Small maintenance commands for the local SQLite store."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from src.config import get_settings
from src.storage.db import get_session
from src.storage.models import Article


def mark_stale_pending_alerts_sent(max_age_hours: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    session = get_session()
    try:
        rows = session.execute(
            select(Article).where(
                Article.alert_sent == False,  # noqa: E712 - SQLAlchemy comparison
                Article.published_at < cutoff,
            )
        ).scalars().all()
        for article in rows:
            article.alert_sent = True
        session.commit()
        return len(rows)
    finally:
        session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="stock-news-alerts maintenance")
    parser.add_argument(
        "--mark-stale-pending-sent",
        action="store_true",
        help="Mark pending alerts older than MAX_NEWS_AGE_HOURS as sent so they are never delivered.",
    )
    args = parser.parse_args()

    settings = get_settings()
    if args.mark_stale_pending_sent:
        count = mark_stale_pending_alerts_sent(settings.max_news_age_hours)
        print(f"Marked {count} stale pending alert(s) as sent.")
    else:
        parser.print_help()

"""Engine/session factory and data-access helpers for the SQLite store."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from src.config import get_settings
from src.storage.models import ApiUsage, Article, Base, TickerFetchLog, utcnow

_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(f"sqlite:///{settings.db_path}", future=True)
        _ensure_schema(_engine)
    return _engine


def get_session() -> Session:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), future=True)
    return _SessionLocal()


def init_db() -> None:
    Base.metadata.create_all(get_engine())
    _ensure_schema(get_engine())


def _ensure_schema(engine) -> None:
    """Create tables and add lightweight SQLite columns for existing DB files."""
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    if "articles" not in inspector.get_table_names():
        return

    existing = {col["name"] for col in inspector.get_columns("articles")}
    additions = {
        "materiality_score": "ALTER TABLE articles ADD COLUMN materiality_score FLOAT DEFAULT 0.0",
        "impact_horizon": "ALTER TABLE articles ADD COLUMN impact_horizon VARCHAR DEFAULT 'unknown'",
        "source_quality": "ALTER TABLE articles ADD COLUMN source_quality FLOAT DEFAULT 0.0",
        "is_material": "ALTER TABLE articles ADD COLUMN is_material BOOLEAN DEFAULT 0",
        "category": "ALTER TABLE articles ADD COLUMN category VARCHAR DEFAULT ''",
        "impact_tier": "ALTER TABLE articles ADD COLUMN impact_tier VARCHAR DEFAULT ''",
    }
    with engine.begin() as conn:
        for column, ddl in additions.items():
            if column not in existing:
                conn.execute(text(ddl))


def headline_hash(headline: str) -> str:
    normalized = headline.strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def article_exists(session: Session, url: str, h_hash: str) -> bool:
    stmt = select(Article.id).where(
        (Article.url == url) | (Article.headline_hash == h_hash)
    )
    return session.execute(stmt).first() is not None


def save_article(
    session: Session,
    *,
    ticker: str,
    headline: str,
    url: str,
    source: str,
    published_at: datetime,
    event_type: str,
    direction: str,
    confidence: float,
    reasoning: str,
    materiality_score: float = 0.0,
    impact_horizon: str = "unknown",
    source_quality: float = 0.0,
    is_material: bool = False,
    category: str = "",
    impact_tier: str = "",
) -> Article:
    article = Article(
        ticker=ticker,
        headline=headline,
        headline_hash=headline_hash(headline),
        url=url,
        source=source,
        published_at=published_at,
        category=category,
        impact_tier=impact_tier,
        event_type=event_type,
        direction=direction,
        confidence=confidence,
        materiality_score=materiality_score,
        impact_horizon=impact_horizon,
        source_quality=source_quality,
        is_material=is_material,
        reasoning=reasoning,
        alert_sent=False,
    )
    session.add(article)
    session.commit()
    session.refresh(article)
    return article


def mark_alert_sent(session: Session, article_id: int) -> None:
    article = session.get(Article, article_id)
    if article is not None:
        article.alert_sent = True
        session.commit()


def get_pending_alert_articles(
    session: Session,
    *,
    confidence_threshold: float,
    min_source_quality: float,
    min_published_at: datetime | None = None,
    limit: int = 25,
) -> list[Article]:
    filters = [
        Article.alert_sent == False,  # noqa: E712 - SQLAlchemy comparison
        Article.is_material == True,  # noqa: E712 - SQLAlchemy comparison
        Article.confidence >= confidence_threshold,
        Article.source_quality >= min_source_quality,
        Article.direction != "neutral",
        Article.event_type.not_in(["other", "classification_failed", "procedural"]),
    ]
    if min_published_at is not None:
        filters.append(Article.published_at >= min_published_at)

    stmt = (
        select(Article)
        .where(*filters)
        .order_by(Article.created_at.asc())
        .limit(limit)
    )
    return list(session.execute(stmt).scalars())


def get_todays_stats(session: Session) -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    stmt = select(Article).where(Article.created_at >= today)
    articles = session.execute(stmt).scalars().all()
    return {
        "processed": len(articles),
        "alerts_sent": sum(1 for a in articles if a.alert_sent),
    }


def _today_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def get_api_usage_today(session: Session, source: str) -> int:
    today = _today_str()
    stmt = select(ApiUsage).where(ApiUsage.date == today, ApiUsage.source == source)
    row = session.execute(stmt).scalar_one_or_none()
    return row.count if row else 0


def increment_api_usage(session: Session, source: str) -> None:
    today = _today_str()
    stmt = select(ApiUsage).where(ApiUsage.date == today, ApiUsage.source == source)
    row = session.execute(stmt).scalar_one_or_none()
    if row is None:
        row = ApiUsage(date=today, source=source, count=1)
        session.add(row)
    else:
        row.count += 1
    session.commit()


def get_last_ticker_fetch(session: Session, ticker: str, source: str) -> datetime | None:
    stmt = select(TickerFetchLog).where(
        TickerFetchLog.ticker == ticker, TickerFetchLog.source == source
    )
    row = session.execute(stmt).scalar_one_or_none()
    return row.last_fetched_at if row else None


def set_last_ticker_fetch(session: Session, ticker: str, source: str) -> None:
    stmt = select(TickerFetchLog).where(
        TickerFetchLog.ticker == ticker, TickerFetchLog.source == source
    )
    row = session.execute(stmt).scalar_one_or_none()
    now = utcnow()
    if row is None:
        row = TickerFetchLog(ticker=ticker, source=source, last_fetched_at=now)
        session.add(row)
    else:
        row.last_fetched_at = now
    session.commit()

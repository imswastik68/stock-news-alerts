"""SQLAlchemy models for the stock-news-alerts SQLite database."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    headline: Mapped[str] = mapped_column(String)
    # sha256 of lowercased/stripped headline — dedupes the same story appearing
    # under different URLs across sources (NSE filing vs. news aggregator rewrite).
    headline_hash: Mapped[str] = mapped_column(String, index=True)
    url: Mapped[str] = mapped_column(String, unique=True)
    source: Mapped[str] = mapped_column(String)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Exchange announcement category (NSE `desc`) and its mapped impact tier
    # ("high"/"medium"/"drop"). Empty/"" for media-sourced articles.
    category: Mapped[str] = mapped_column(String, default="")
    impact_tier: Mapped[str] = mapped_column(String, default="")
    event_type: Mapped[str] = mapped_column(String)
    direction: Mapped[str] = mapped_column(String)
    confidence: Mapped[float] = mapped_column(Float)
    materiality_score: Mapped[float] = mapped_column(Float, default=0.0)
    impact_horizon: Mapped[str] = mapped_column(String, default="unknown")
    source_quality: Mapped[float] = mapped_column(Float, default=0.0)
    is_material: Mapped[bool] = mapped_column(Boolean, default=False)
    reasoning: Mapped[str] = mapped_column(String)
    alert_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiUsage(Base):
    """Tracks daily request counts per source (currently used for the NewsAPI
    100 req/day free-tier budget) and, via last_ticker_fetch, the last time each
    ticker was queried against a rate-limited source."""

    __tablename__ = "api_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(String, index=True)  # YYYY-MM-DD (UTC)
    source: Mapped[str] = mapped_column(String, index=True)
    count: Mapped[int] = mapped_column(Integer, default=0)


class TickerFetchLog(Base):
    """Last time a rate-limited source (NewsAPI) was queried for a given ticker,
    used to enforce a minimum interval between queries per ticker."""

    __tablename__ = "ticker_fetch_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    source: Mapped[str] = mapped_column(String, index=True)
    last_fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

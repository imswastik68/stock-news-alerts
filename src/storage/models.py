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
    # Outcome tracking (filled in later by src/scoring/outcomes.py for alerted
    # rows): forward % return of the stock over N trading days from the alert.
    # NULL until that horizon has matured. Feeds the calibrated confidence model.
    ret_1d: Mapped[float | None] = mapped_column(Float, nullable=True)
    ret_3d: Mapped[float | None] = mapped_column(Float, nullable=True)
    ret_5d: Mapped[float | None] = mapped_column(Float, nullable=True)
    # NIFTY 50 (^NSEI) forward % return over the SAME window as ret_Nd, so
    # evaluate.py can compute alpha = ret_Nd - idx_ret_Nd. Raw returns alone
    # can't tell "our news call added value" apart from "the whole market
    # moved" — this is what separates the two. Independently nullable: a stock
    # return can mature (and be recorded) before/without a successful index
    # fetch, backfilled on a later pass.
    idx_ret_1d: Mapped[float | None] = mapped_column(Float, nullable=True)
    idx_ret_3d: Mapped[float | None] = mapped_column(Float, nullable=True)
    idx_ret_5d: Mapped[float | None] = mapped_column(Float, nullable=True)

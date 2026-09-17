"""SQLAlchemy models for the stock-news-alerts SQLite database."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    # Human-readable company name (from the exchange filing). Kept alongside the
    # ticker so a BSE-only scrip that never resolves to an NSE symbol — ticker
    # is then a bare number like '532933.BO' — can still show its name in the
    # alert instead of a meaningless code. NULL for older rows / when unknown.
    company_name: Mapped[str | None] = mapped_column(String, nullable=True)
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
    # Set only on event_type="classification_failed": which backend failed and
    # why ("gemini: 429 rate limited; groq: NotFoundError 404: ..."). Exists
    # because `reasoning` carried one constant string for all 5223 failed rows,
    # so the single largest loss in the pipeline could only be diagnosed by
    # reading GitHub Actions logs. NULL for successfully classified rows.
    classification_error: Mapped[str | None] = mapped_column(String, nullable=True)
    # How many times the LLM has been asked to classify this filing. A failure
    # used to be permanent: the row was stored, article_exists() then matched it
    # forever, and a filing lost to a transient 429 was never seen again. Rows
    # under MAX_CLASSIFY_ATTEMPTS are re-offered while still inside the
    # freshness window (see src/pipeline.py). Nullable to match what the
    # ALTER TABLE migration can produce on an existing DB file; readers treat
    # NULL as 0.
    classify_attempts: Mapped[int | None] = mapped_column(Integer, default=0, nullable=True)
    # True once the row has been HANDLED, which is not the same as delivered:
    # a suppressed duplicate is marked too, so it is never retried. Check
    # suppressed_reason to tell the two apart.
    alert_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    # NULL = actually pushed to Telegram. Otherwise why it was withheld
    # ("duplicate", "priced_in"). Measurement MUST exclude non-NULL rows: a
    # suppressed duplicate's forward return is measured from a base after the
    # move it duplicates has already happened, so it is noise. Measured
    # 2026-09-17: the 28% of alert_sent rows that were never delivered hit 47.8%
    # at 1d against 64.4% for delivered ones, dragging the reported track record
    # down by ~5 points and feeding the same noise into confidence calibration.
    suppressed_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Which entry price ret_Nd below is measured from — set by
    # src/scoring/outcomes.py when the return is recorded:
    #   "close"      news was already public when that session's close printed,
    #                so the close is a price an order could have been filled at.
    #   "next_open"  news broke AFTER the close, so the return starts at the NEXT
    #                session's open.
    #   "next_close" same case, but the scrip is BSE-only and bhavcopy carries no
    #                open prices, so entry is the next session's close instead —
    #                honest, just more conservative than the open.
    #   NULL         legacy row measured before 2026-09-17, always on a close
    #                base. For an after-hours row that base pre-dates the news,
    #                so the value silently contains an uncapturable overnight gap.
    # This is not bookkeeping: measuring an after-hours alert from the prior
    # close books the gap reaction as alpha. On 271 delivered alerts the
    # after-hours bucket showed +1.75% avg 1d alpha that way, against +0.26% for
    # rows with a genuinely tradable base — nearly the whole apparent edge.
    entry_basis: Mapped[str | None] = mapped_column(String, nullable=True)
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


class BseClose(Base):
    """One BSE scrip's official EOD close for one trading day, from BSE's own
    bhavcopy (src/ingestion/bse_bhavcopy.py). Exists because yfinance prices
    nothing for a large share of BSE scrip codes, which silently made a third of
    alerted rows permanently unmeasurable."""

    __tablename__ = "bse_closes"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    scrip: Mapped[str] = mapped_column(String, primary_key=True)
    close: Mapped[float] = mapped_column(Float)


class BseBhavcopyDay(Base):
    """Marker: this date's bhavcopy has been resolved, and whether it was a
    trading day at all. Needed because BSE serves HTTP 200 + an HTML page for
    weekends/holidays rather than a 404 — without recording the negative, every
    cycle would re-fetch every non-trading day forever."""

    __tablename__ = "bse_bhavcopy_days"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    is_trading_day: Mapped[bool] = mapped_column(Boolean, default=False)

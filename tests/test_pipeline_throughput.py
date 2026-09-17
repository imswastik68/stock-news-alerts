"""The per-cycle classification cap must be spent on filings nobody has read yet.

Measured on a five-hour production run (GitHub Actions job 105125895353,
2026-09-17 08:08-13:09 UTC): all 132 cycles logged `fetched=10`, and 85 of them
logged `new=0`. The cap was applied to the whole 48h feed BEFORE the
already-stored check, so the ten slots went to whatever ranked highest — nearly
always filings classified hours earlier. Of the 84 filings that did get through,
67 (80%) failed classification, and each failure was permanent: the
classification_failed row satisfied the dedupe check forever after.

These tests pin both halves of the fix — dedupe before the cap, and a bounded
retry so a transient 429 delays a filing instead of deleting it.
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from src import pipeline
from src.ingestion.common import RawArticle
from src.storage.db import (
    find_article,
    handled_article_keys,
    headline_hash,
    is_retryable_failure,
    save_article,
)
from src.storage.models import Article, Base


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _raw(n: int, minutes_ago: int = 1) -> RawArticle:
    return RawArticle(
        ticker=f"T{n}.NS",
        headline=f"headline {n}",
        summary="",
        url=f"https://example.com/{n}",
        source="nse_announcements",
        published_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        category="Acquisition",
    )


def _store(session, raw: RawArticle, event_type="partnership_contract", attempts=1):
    return save_article(
        session,
        ticker=raw.ticker,
        headline=raw.headline,
        url=raw.url,
        source=raw.source,
        published_at=raw.published_at,
        category=raw.category,
        impact_tier="high",
        event_type=event_type,
        direction="bullish",
        confidence=0.6,
        reasoning="x",
        classify_attempts=attempts,
    )


# ── dedupe before the cap ────────────────────────────────────────────────────


def test_already_classified_filings_are_dropped(session):
    articles = [_raw(i) for i in range(5)]
    for raw in articles[:3]:
        _store(session, raw)

    kept = pipeline._drop_already_handled(session, articles)

    assert [a.url for a in kept] == [articles[3].url, articles[4].url]


def test_a_filing_stored_under_a_different_url_is_still_dropped(session):
    """Dedupe is (url OR headline hash) — a cross-exchange re-filing of the same
    headline must not consume a second classification slot."""
    original = _raw(1)
    _store(session, original)
    same_story = _raw(1)
    same_story.url = "https://other-exchange.example.com/1"

    assert pipeline._drop_already_handled(session, [same_story]) == []


def test_failed_classification_is_offered_again(session):
    raw = _raw(1)
    _store(session, raw, event_type="classification_failed", attempts=1)

    kept = pipeline._drop_already_handled(session, [raw])

    assert [a.url for a in kept] == [raw.url]


def test_failed_classification_is_written_off_at_the_attempt_ceiling(session):
    raw = _raw(1)
    _store(
        session, raw, event_type="classification_failed",
        attempts=pipeline.MAX_CLASSIFY_ATTEMPTS,
    )

    assert pipeline._drop_already_handled(session, [raw]) == []


def test_legacy_failed_rows_with_no_attempt_count_are_retried(session):
    """Rows written before classify_attempts existed read back as NULL."""
    raw = _raw(1)
    stored = _store(session, raw, event_type="classification_failed", attempts=0)
    stored.classify_attempts = None
    session.commit()

    urls, hashes = handled_article_keys(
        session, [raw.url], [headline_hash(raw.headline)], pipeline.MAX_CLASSIFY_ATTEMPTS
    )
    assert urls == set() and hashes == set()
    assert is_retryable_failure(stored, pipeline.MAX_CLASSIFY_ATTEMPTS)


def test_the_cycle_cap_is_spent_on_unseen_filings(session, monkeypatch):
    """The regression this file exists for.

    25 filings in the feed, 20 already classified, cap 10. Trimming first (the
    old order) hands the classifier ten already-stored rows and zero new ones.
    """
    feed = [_raw(i, minutes_ago=25 - i) for i in range(25)]
    for raw in feed[:20]:
        _store(session, raw)

    monkeypatch.setattr(pipeline, "fetch_nse_market_wide", lambda: list(feed))
    monkeypatch.setattr(pipeline, "fetch_nse_rss", list)
    monkeypatch.setattr(pipeline, "fetch_bse_rss", list)
    settings = types.SimpleNamespace(max_news_age_hours=48, max_articles_per_cycle=10)

    gathered = pipeline._gather_articles(session, settings)

    assert {a.url for a in gathered} == {a.url for a in feed[20:]}


# ── a failure delays a filing, it does not delete it ─────────────────────────


def test_retry_rewrites_the_failed_row_instead_of_inserting_a_second_one(session):
    raw = _raw(1)
    failed = _store(session, raw, event_type="classification_failed", attempts=1)

    save_article(
        session,
        ticker=raw.ticker,
        headline=raw.headline,
        url=raw.url,
        source=raw.source,
        published_at=raw.published_at,
        category=raw.category,
        impact_tier="high",
        event_type="partnership_contract",
        direction="bullish",
        confidence=0.7,
        reasoning="order win",
        classify_attempts=2,
        existing=failed,
    )

    assert session.execute(select(func.count(Article.id))).scalar_one() == 1
    row = find_article(session, raw.url, headline_hash(raw.headline))
    assert row.event_type == "partnership_contract"
    assert row.classify_attempts == 2
    assert row.classification_error is None


def test_failure_records_which_backend_failed_and_why(session, monkeypatch):
    raw = _raw(1)
    monkeypatch.setattr(pipeline, "classify", lambda _: None)
    monkeypatch.setattr(
        pipeline, "classification_failure_reason", lambda: "gemini: 429 rate limited"
    )
    settings = types.SimpleNamespace(
        min_materiality_score=0.65,
        alert_confidence_threshold=0.7,
        min_source_quality_for_alerts=0.55,
        high_tier_confidence_threshold=0.35,
        directionally_unreliable_event_types=frozenset(),
        dedup_window_hours=3.0,
        dedup_similarity_threshold=0.65,
        dedup_same_event_window_hours=24.0,
        priced_in_drift_threshold_pct=8.0,
    )

    outcome = pipeline._process_article(session, None, settings, raw)
    assert outcome["new"] is True and outcome["classified"] is False

    row = find_article(session, raw.url, headline_hash(raw.headline))
    assert row.event_type == "classification_failed"
    assert row.classification_error == "gemini: 429 rate limited"
    assert row.classify_attempts == 1

    # Second pass: same filing, still retryable, attempts climb rather than
    # inserting a duplicate row.
    outcome = pipeline._process_article(session, None, settings, raw)
    assert outcome["new"] is False and outcome["retried"] is True
    session.refresh(row)
    assert row.classify_attempts == 2
    assert session.execute(select(func.count(Article.id))).scalar_one() == 1

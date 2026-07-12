"""
Orchestrates one full cycle: ingest -> dedupe -> classify -> score -> store -> alert.

Every article is handled in its own try/except so one bad article (malformed
LLM output, a storage hiccup, a Telegram send failure) never aborts the rest of
the cycle. Run `python -m src.pipeline --once` for a single cycle (used for
manual testing and by the --once CLI flag); scheduler.py calls run_pipeline()
on an interval for the long-running service.
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta, timezone

from src.classification.classifier import classify, is_rate_limited, reset_cycle_state
from src.config import configure_logging, get_settings
from src.ingestion.common import RawArticle
from src.ingestion.google_news import fetch_google_news
from src.ingestion.indian_rss import fetch_indian_rss
from src.ingestion.newsapi import fetch_newsapi
from src.ingestion.bse_announcements import fetch_bse_announcements
from src.ingestion.nse_announcements import fetch_nse_announcements, fetch_nse_market_wide
from src.ingestion.pdf_extract import extract_pdf_text
from src.scoring.confidence import StaticTableConfidenceProvider
from src.scoring.impact import DROP, HIGH, category_impact
from src.scoring.materiality_filter import should_use_llm
from src.scoring.source_quality import get_source_quality, is_directional_material_alert
from src.storage.db import (
    article_exists,
    get_pending_alert_articles,
    get_session,
    headline_hash,
    save_article,
    mark_alert_sent,
)
from src.alerting.telegram_bot import send_alert

logger = logging.getLogger(__name__)

_EXCLUDED_FROM_ALERTS = {"other", "classification_failed"}
_SOURCE_PRIORITY = {
    "nse_announcements": 0,
    "bse_announcements": 1,
    "moneycontrol": 2,
    "economic_times": 3,
    "newsapi": 4,
    "google_news": 5,
}


_IMPACT_RANK = {HIGH: 0, "medium": 1}


def _trim_articles_for_cycle(articles: list[RawArticle], max_articles: int) -> list[RawArticle]:
    if max_articles <= 0 or len(articles) <= max_articles:
        return articles

    # HIGH-impact categories first (order wins, M&A, results, ratings, bonus,
    # dividend, penalties), then by recency. Under the free-tier per-cycle cap
    # this is what matters most: a genuine catalyst must not wait behind a queue
    # of procedural "General Updates"/"Newspaper Publication" filings. Source
    # priority breaks ties (exchange filings over media).
    def _rank(a: RawArticle):
        tier = category_impact(a.category) if a.category else "medium"
        return (
            _IMPACT_RANK.get(tier, 1),
            _SOURCE_PRIORITY.get(a.source, 99),
            -a.published_at.timestamp(),
        )

    ordered = sorted(articles, key=_rank)
    logger.info(
        "pipeline: limiting classification workload from %d to %d article(s) "
        "(high-impact categories first)",
        len(articles),
        max_articles,
    )
    return ordered[:max_articles]


def _filter_recent_articles(articles: list[RawArticle], max_age_hours: int) -> list[RawArticle]:
    if max_age_hours <= 0:
        return articles
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    recent: list[RawArticle] = []
    dropped = 0
    for article in articles:
        published_at = article.published_at
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        if published_at >= cutoff:
            recent.append(article)
        else:
            dropped += 1
    if dropped:
        logger.info(
            "pipeline: dropped %d stale article(s) older than %dh",
            dropped,
            max_age_hours,
        )
    return recent


def _gather_market_wide(settings) -> list[RawArticle]:
    """Market-wide mode: the exchange-filing backbone — every company's NSE
    filing. No per-ticker media (media needs a company name to map to a ticker,
    which only exists in watchlist mode)."""
    articles: list[RawArticle] = []
    try:
        articles.extend(fetch_nse_market_wide())
    except Exception as exc:
        logger.error("pipeline: nse_market_wide fetch crashed: %s", exc)

    # BSE-exclusive scrips (NSE market-wide misses BSE-only stocks). Add only
    # BSE-EXCLUSIVE names to bse_watchlist.yaml — dual-listed ones are already in
    # the NSE feed and would double-alert.
    if settings.bse_watchlist:
        try:
            articles.extend(fetch_bse_announcements(settings.bse_watchlist, hours_back=36))
        except Exception as exc:
            logger.error("pipeline: bse_announcements fetch crashed: %s", exc)

    # Hard-drop always-procedural categories BEFORE the per-cycle cap, so the
    # limited classification budget goes to potentially-material filings, not
    # trading-window notices and compliance certificates.
    kept = [a for a in articles if category_impact(a.category) != DROP]
    if len(kept) < len(articles):
        logger.info(
            "pipeline: dropped %d always-procedural filing(s) pre-LLM",
            len(articles) - len(kept),
        )
    return kept


def _gather_watchlist(settings) -> list[RawArticle]:
    tickers = [e.ticker for e in settings.watchlist]
    name_pairs = [(e.ticker, e.name) for e in settings.watchlist]

    articles: list[RawArticle] = []

    try:
        articles.extend(fetch_nse_announcements(tickers))
    except Exception as exc:
        logger.error("pipeline: nse_announcements fetch crashed: %s", exc)

    try:
        articles.extend(fetch_bse_announcements(settings.watchlist))
    except Exception as exc:
        logger.error("pipeline: bse_announcements fetch crashed: %s", exc)

    try:
        articles.extend(fetch_indian_rss(settings.watchlist))
    except Exception as exc:
        logger.error("pipeline: indian_rss fetch crashed: %s", exc)

    try:
        articles.extend(fetch_google_news(name_pairs))
    except Exception as exc:
        logger.error("pipeline: google_news fetch crashed: %s", exc)

    session = get_session()
    try:
        for ticker, name in name_pairs:
            try:
                articles.extend(fetch_newsapi(session, ticker, name))
            except Exception as exc:
                logger.error("pipeline: newsapi fetch crashed for %s: %s", ticker, exc)
    finally:
        session.close()

    return articles


def _gather_articles(settings) -> list[RawArticle]:
    if settings.coverage_mode == "watchlist":
        articles = _gather_watchlist(settings)
    else:
        articles = _gather_market_wide(settings)

    articles = _filter_recent_articles(articles, settings.max_news_age_hours)
    return _trim_articles_for_cycle(articles, settings.max_articles_per_cycle)


def _prefilter_passes(raw: RawArticle, impact_tier: str) -> bool:
    """Decide whether an article is worth an (LLM) classification call.
    Exchange filings are gated by impact tier (drop procedural categories);
    media by the keyword materiality prefilter."""
    if raw.category:  # exchange filing
        return impact_tier != DROP
    return should_use_llm(raw)


def _process_article(session, confidence_provider, settings, raw: RawArticle) -> dict:
    """Returns a small outcome dict for cycle-summary counting. Never raises —
    all failure modes are caught and logged."""
    outcome = {"new": False, "classified": False, "alerted": False}

    h_hash = headline_hash(raw.headline)
    if article_exists(session, raw.url, h_hash):
        return outcome
    outcome["new"] = True

    source_quality = get_source_quality(raw.source)
    impact_tier = category_impact(raw.category) if raw.category else ""

    if not _prefilter_passes(raw, impact_tier):
        try:
            save_article(
                session,
                ticker=raw.ticker,
                headline=raw.headline,
                url=raw.url,
                source=raw.source,
                published_at=raw.published_at,
                category=raw.category,
                impact_tier=impact_tier,
                event_type="procedural" if raw.category else "other",
                direction="neutral",
                confidence=0.0,
                materiality_score=0.0,
                impact_horizon="unknown",
                source_quality=source_quality,
                is_material=False,
                reasoning="Filtered out before LLM (procedural filing / non-material media).",
            )
        except Exception as exc:
            logger.error("pipeline: failed to store prefilter-skipped article: %s", exc)
        return outcome

    # Read the filing's PDF so we classify on the actual content (results
    # numbers, order value, rating) rather than NSE's generic category tag. This
    # is what lets a material filing hidden under "General Updates" get seen.
    if raw.category and raw.attachment_url and not raw.body:
        try:
            body = extract_pdf_text(raw.attachment_url)
            if body:
                raw.body = body
                logger.info("pipeline: read PDF (%d chars) for %s", len(body), raw.ticker)
            else:
                logger.info("pipeline: PDF unreadable for %s (scanned/blocked)", raw.ticker)
        except Exception as exc:
            logger.warning("pipeline: pdf extract crashed for %s: %s", raw.ticker, exc)

    try:
        result = classify(raw)
    except Exception as exc:
        logger.error("pipeline: classify() crashed for %r: %s", raw.headline[:80], exc)
        result = None

    if result is None:
        try:
            save_article(
                session,
                ticker=raw.ticker,
                headline=raw.headline,
                url=raw.url,
                source=raw.source,
                published_at=raw.published_at,
                category=raw.category,
                impact_tier=impact_tier,
                event_type="classification_failed",
                direction="neutral",
                confidence=0.0,
                reasoning="LLM classification failed or backend unreachable.",
            )
        except Exception as exc:
            logger.error("pipeline: failed to store classification_failed article: %s", exc)
        return outcome

    outcome["classified"] = True
    confidence = confidence_provider.get_confidence(result)
    # A HIGH-impact exchange category is material by itself, regardless of the
    # LLM's own materiality estimate.
    is_material = impact_tier == HIGH or result.materiality_score >= settings.min_materiality_score

    # Prefer the LLM's clean one-line headline (built from the PDF content) over
    # NSE's boilerplate title — this is what makes the alert read like the pro
    # platforms ("Reports FY26 profit up 29%…" vs "informed the Exchange about…").
    display_headline = result.headline.strip() if result.headline.strip() else raw.headline

    try:
        article = save_article(
            session,
            ticker=raw.ticker,
            headline=display_headline,
            url=raw.url,
            source=raw.source,
            published_at=raw.published_at,
            category=raw.category,
            impact_tier=impact_tier,
            event_type=result.event_type,
            direction=result.direction,
            confidence=confidence,
            materiality_score=result.materiality_score,
            impact_horizon=result.impact_horizon,
            source_quality=source_quality,
            is_material=is_material,
            reasoning=result.reason,
        )
    except Exception as exc:
        logger.error("pipeline: failed to store article %r: %s", raw.headline[:80], exc)
        return outcome

    if is_directional_material_alert(
        result,
        confidence=confidence,
        source_quality=source_quality,
        alert_confidence_threshold=settings.alert_confidence_threshold,
        min_materiality_score=settings.min_materiality_score,
        min_source_quality_for_alerts=settings.min_source_quality_for_alerts,
        excluded_event_types=_EXCLUDED_FROM_ALERTS,
        impact_tier=impact_tier,
    ):
        try:
            if send_alert(article):
                mark_alert_sent(session, article.id)
                outcome["alerted"] = True
        except Exception as exc:
            logger.error("pipeline: send_alert crashed for %r: %s", raw.headline[:80], exc)

    return outcome


def _send_pending_alerts(session, settings) -> int:
    if settings.dry_run:
        return 0

    sent = 0
    pending = get_pending_alert_articles(
        session,
        confidence_threshold=settings.alert_confidence_threshold,
        min_source_quality=settings.min_source_quality_for_alerts,
        min_published_at=datetime.now(timezone.utc) - timedelta(hours=settings.max_news_age_hours),
    )
    for article in pending:
        try:
            if send_alert(article):
                mark_alert_sent(session, article.id)
                sent += 1
        except Exception as exc:
            logger.error("pipeline: pending send_alert crashed for %r: %s", article.headline[:80], exc)
    if sent:
        logger.info("pipeline: sent %d pending alert(s)", sent)
    return sent


def run_pipeline() -> dict:
    settings = get_settings()
    reset_cycle_state()

    fetched = _gather_articles(settings)
    logger.info("pipeline: %d article(s) fetched this cycle", len(fetched))

    confidence_provider = StaticTableConfidenceProvider(settings.confidence_base_rates)
    session = get_session()
    totals = {"fetched": len(fetched), "new": 0, "classified": 0, "alerted": 0}

    try:
        for raw in fetched:
            if is_rate_limited():
                logger.warning("pipeline: stopping classification early because Groq is rate limited")
                break
            try:
                outcome = _process_article(session, confidence_provider, settings, raw)
            except Exception as exc:
                logger.error("pipeline: unexpected error processing %r: %s", raw.headline[:80], exc)
                continue
            for key in ("new", "classified", "alerted"):
                totals[key] += int(outcome[key])
        totals["alerted"] += _send_pending_alerts(session, settings)
    finally:
        session.close()

    logger.info(
        "pipeline: cycle complete — fetched=%d new=%d classified=%d alerted=%d",
        totals["fetched"], totals["new"], totals["classified"], totals["alerted"],
    )
    return totals


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="stock-news-alerts pipeline")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    args = parser.parse_args()

    configure_logging()

    if args.once:
        run_pipeline()
    else:
        print("Run with --once for a single cycle, or use scheduler.py for continuous polling.")

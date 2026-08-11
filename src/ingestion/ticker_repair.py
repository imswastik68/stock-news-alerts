"""
Re-resolve tickers that were stored as a raw company name.

Ingestion resolves a filing's company to a ticker ONCE, at the moment the
article is stored. When the NSE symbol master is momentarily unreachable (its
archives are genuinely flaky — the fallback in symbol_master.py exists for
exactly that), the resolver returns nothing and exchange_rss falls back to
keeping the company name as the "ticker". That name is then permanent: nothing
ever revisits it, so the row can never be priced, never matures an outcome, and
is silently excluded from the track record.

Confirmed against production 2026-08-11: nine alerted rows carried a company
name as their ticker, and six of them resolve correctly on a later attempt —
"Antony Waste Handling Cell Limited" -> AWHCL, "Chavda Infra Limited" -> CHAVDA,
"Anlon Technology Solutions Limited" -> ANLON, "GP Eco Solutions India Limited"
-> GPECO, "Deccan Transcon Leasing Limited" -> DECCANTRAN, plus one more. Those
were never unresolvable companies; they were transient outages frozen into the
data.

So this is a repair pass, not a resolver: it finds rows whose ticker isn't a
ticker at all and retries them against the (by now healthy) symbol master. Only
rows still inside the outcome-tracking window are worth repairing — older ones
can no longer mature a return, so fixing them changes nothing.

Guarded so a second outage can't make things worse: if the master is
unavailable right now, the pass does nothing at all rather than "confirming"
that everything is unresolvable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.ingestion.symbol_master import is_master_available, resolve_nse_symbol
from src.storage.models import Article

logger = logging.getLogger(__name__)

# Only repair rows that can still mature an outcome; matches the tracking
# window in src/scoring/outcomes.py.
_MAX_REPAIR_AGE_DAYS = 20


def looks_unresolved(ticker: str) -> bool:
    """True if `ticker` is a company name rather than a ticker.

    A real ticker is '<SYMBOL>.NS' or '<scrip>.BO' — no spaces. Company names
    always contain a space in practice ("Chavda Infra Limited"), and the check
    stays deliberately conservative: anything ending in a known suffix is left
    alone even if it looks odd, so a valid ticker can never be "repaired" into
    something else."""
    if not ticker or not ticker.strip():
        return False
    if ticker.endswith((".NS", ".BO")):
        return False
    return " " in ticker.strip()


def repair_unresolved_tickers(session: Session, limit: int = 50) -> int:
    """Retry company-name tickers against the symbol master. Returns how many
    rows were repaired. No-ops when the master is unavailable."""
    if not is_master_available():
        logger.debug("ticker_repair: symbol master unavailable, skipping")
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=_MAX_REPAIR_AGE_DAYS)
    # Filter for unresolved tickers IN SQL, not in Python after a LIMIT. Doing it
    # the other way round only ever inspects the first N rows of the whole table
    # — almost all of which have perfectly good tickers — so the rows that
    # actually need repair are never reached. Caught by running this against the
    # real production DB: it repaired 0 of 136 eligible rows.
    stmt = (
        select(Article)
        .where(
            Article.published_at >= cutoff,
            Article.ticker.like("% %"),          # company names contain spaces
            ~Article.ticker.like("%.NS"),
            ~Article.ticker.like("%.BO"),
        )
        .limit(limit)
    )

    repaired = 0
    for article in session.execute(stmt).scalars():
        if repaired >= limit:
            break
        if not looks_unresolved(article.ticker):
            continue
        symbol = resolve_nse_symbol(article.ticker)
        if not symbol:
            continue
        # Keep the original name — it's the company name, which is exactly what
        # the alert wants to display for a non-NSE listing anyway.
        if not article.company_name:
            article.company_name = article.ticker
        logger.info("ticker_repair: %r -> %s.NS", article.ticker, symbol)
        article.ticker = f"{symbol}.NS"
        repaired += 1

    if repaired:
        session.commit()
        logger.info("ticker_repair: repaired %d ticker(s)", repaired)
    return repaired

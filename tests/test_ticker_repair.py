"""Tests for re-resolving tickers frozen as company names.

A transient NSE symbol-master outage makes exchange_rss fall back to storing the
company name as the ticker, and nothing ever revisited it — so the row could
never be priced or matured. Confirmed in production: nine rows carried a company
name, six of which resolve fine on a retry.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.ingestion.ticker_repair import looks_unresolved, repair_unresolved_tickers
from src.storage.db import save_article
from src.storage.models import Article, Base


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _add(session, ticker, days_ago=2):
    return save_article(
        session, ticker=ticker, headline=f"{ticker} headline", url=f"u-{ticker}",
        source="nse_announcements",
        published_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        category="Acquisition", impact_tier="high", event_type="partnership_contract",
        direction="bullish", confidence=0.5, materiality_score=0.8,
        impact_horizon="1_3_days", source_quality=1.0, is_material=True, reasoning="x",
    )


# ── looks_unresolved ─────────────────────────────────────────────────────────

def test_company_names_are_detected_as_unresolved():
    assert looks_unresolved("Antony Waste Handling Cell Limited")
    assert looks_unresolved("Chavda Infra Limited")


def test_real_tickers_are_never_flagged():
    # The conservative half of the check: a valid ticker must never be
    # "repaired" into something else.
    assert not looks_unresolved("RELIANCE.NS")
    assert not looks_unresolved("543254.BO")
    assert not looks_unresolved("")


# ── repair pass ──────────────────────────────────────────────────────────────

def test_resolvable_company_name_is_repaired(session):
    a = _add(session, "Antony Waste Handling Cell Limited")
    with patch("src.ingestion.ticker_repair.is_master_available", return_value=True), \
         patch("src.ingestion.ticker_repair.resolve_nse_symbol", return_value="AWHCL"):
        assert repair_unresolved_tickers(session) == 1
    session.refresh(a)
    assert a.ticker == "AWHCL.NS"
    # the original name is preserved for display
    assert a.company_name == "Antony Waste Handling Cell Limited"


def test_genuinely_unresolvable_name_is_left_alone(session):
    a = _add(session, "Damodar Valley Corporation")  # a PSU, not NSE-listed
    with patch("src.ingestion.ticker_repair.is_master_available", return_value=True), \
         patch("src.ingestion.ticker_repair.resolve_nse_symbol", return_value=None):
        assert repair_unresolved_tickers(session) == 0
    session.refresh(a)
    assert a.ticker == "Damodar Valley Corporation"


def test_no_op_when_symbol_master_is_unavailable(session):
    # The guard that stops a second outage from making things worse.
    _add(session, "Antony Waste Handling Cell Limited")
    with patch("src.ingestion.ticker_repair.is_master_available", return_value=False), \
         patch("src.ingestion.ticker_repair.resolve_nse_symbol") as resolve:
        assert repair_unresolved_tickers(session) == 0
    resolve.assert_not_called()


def test_valid_tickers_are_not_touched(session):
    a = _add(session, "RELIANCE.NS")
    with patch("src.ingestion.ticker_repair.is_master_available", return_value=True), \
         patch("src.ingestion.ticker_repair.resolve_nse_symbol", return_value="WRONG") as r:
        assert repair_unresolved_tickers(session) == 0
    r.assert_not_called()
    session.refresh(a)
    assert a.ticker == "RELIANCE.NS"


def test_rows_past_the_tracking_window_are_skipped(session):
    # Too old to ever mature an outcome, so repairing them changes nothing.
    a = _add(session, "Chavda Infra Limited", days_ago=40)
    with patch("src.ingestion.ticker_repair.is_master_available", return_value=True), \
         patch("src.ingestion.ticker_repair.resolve_nse_symbol", return_value="CHAVDA"):
        assert repair_unresolved_tickers(session) == 0
    session.refresh(a)
    assert a.ticker == "Chavda Infra Limited"


def test_existing_company_name_is_not_overwritten(session):
    a = _add(session, "Chavda Infra Limited")
    a.company_name = "Chavda Infra Ltd (from filing)"
    session.commit()
    with patch("src.ingestion.ticker_repair.is_master_available", return_value=True), \
         patch("src.ingestion.ticker_repair.resolve_nse_symbol", return_value="CHAVDA"):
        repair_unresolved_tickers(session)
    session.refresh(a)
    assert a.ticker == "CHAVDA.NS"
    assert a.company_name == "Chavda Infra Ltd (from filing)"

"""Tests for the official BSE/NSE RSS parsers — the free market-wide path to
BSE (whose JSON API blocks scripted access) and the freshest NSE filings."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from src.ingestion import exchange_rss

_NOW_IST = datetime.now(exchange_rss.IST)
_FRESH = (_NOW_IST - timedelta(hours=1)).strftime("%d-%b-%Y %H:%M:%S")
_STALE = (_NOW_IST - timedelta(hours=100)).strftime("%d-%b-%Y %H:%M:%S")

_BSE_XML = f"""<?xml version="1.0"?>
<rss><channel>
<item>
  <title>Antony Waste Handling Cell Ltd (543254)</title>
  <link>https://www.bseindia.com/xml-data/corpfiling/AttachLive/abc.pdf</link>
  <scripcode>543254</scripcode>
  <description>Board approves Rs 5 per share dividend</description>
  <pubDate>{_FRESH}</pubDate>
</item>
<item>
  <title>Old Filing Ltd (999999)</title>
  <link>https://www.bseindia.com/xml-data/corpfiling/AttachLive/old.pdf</link>
  <scripcode>999999</scripcode>
  <description>Stale item, should be filtered by age</description>
  <pubDate>{_STALE}</pubDate>
</item>
</channel></rss>"""

_NSE_XML = f"""<?xml version="1.0"?>
<rss><channel>
<item>
  <title>Reliance Industries Limited</title>
  <link>https://nsearchives.nseindia.com/corporate/RELIANCE_20260713_order.pdf</link>
  <description>Reliance Industries Limited has informed the Exchange about order win |SUBJECT: Awarding of order(s)/contract(s)</description>
  <pubDate>{_FRESH}</pubDate>
</item>
</channel></rss>"""


def _mock_response(xml: str) -> Mock:
    resp = Mock()
    resp.content = xml.encode()
    resp.raise_for_status = Mock()
    return resp


def test_bse_rss_parses_scrip_ticker_and_filters_stale():
    # BSE-only listing (not resolvable to an NSE ticker) — falls back to the
    # numeric scrip code, as before the symbol-resolution wiring was added.
    with patch("src.ingestion.exchange_rss.requests.get", return_value=_mock_response(_BSE_XML)), \
         patch("src.ingestion.exchange_rss.resolve_nse_symbol", return_value=None):
        articles = exchange_rss.fetch_bse_rss(hours_back=36)

    assert len(articles) == 1  # the 100h-old item is dropped
    a = articles[0]
    assert a.ticker == "543254.BO"
    assert a.source == "bse_rss"
    assert "dividend" in a.headline.lower()
    assert a.attachment_url.endswith(".pdf")


def test_bse_rss_dual_listed_company_resolves_to_nse_ticker():
    # Many BSE-only scrip codes have zero Yahoo Finance price data; a company
    # also listed on NSE should use its (priceable) NSE ticker instead.
    with patch("src.ingestion.exchange_rss.requests.get", return_value=_mock_response(_BSE_XML)), \
         patch("src.ingestion.exchange_rss.resolve_nse_symbol", return_value="AWHCL") as mock_resolve:
        articles = exchange_rss.fetch_bse_rss(hours_back=36)

    assert len(articles) == 1
    assert articles[0].ticker == "AWHCL.NS"
    mock_resolve.assert_called_once_with("Antony Waste Handling Cell Ltd")


def test_bse_rss_fetch_failure_returns_empty_list():
    with patch("src.ingestion.exchange_rss.requests.get", side_effect=ConnectionError("down")):
        assert exchange_rss.fetch_bse_rss() == []


def test_nse_rss_extracts_category_from_subject_and_ticker_from_filename():
    # Filename prefix "RELIANCE" is validated against the real symbol list, not
    # just guessed — must be confirmed valid before being used as the ticker.
    with patch("src.ingestion.exchange_rss.requests.get", return_value=_mock_response(_NSE_XML)), \
         patch("src.ingestion.exchange_rss.is_valid_nse_symbol", return_value=True):
        articles = exchange_rss.fetch_nse_rss(hours_back=36)

    assert len(articles) == 1
    a = articles[0]
    assert a.ticker == "RELIANCE.NS"
    assert a.source == "nse_announcements"  # same source class as the date-range API
    assert a.category == "Awarding of order(s)/contract(s)"
    assert "order win" in a.headline.lower()


def test_nse_rss_invalid_filename_prefix_falls_back_to_company_name_resolution():
    # "RELIANCE" from the filename fails validation this time (e.g. a wrong
    # prefix like "AWHCLP") — must fall back to resolving the title (company
    # name) against the symbol master instead of trusting the filename guess.
    with patch("src.ingestion.exchange_rss.requests.get", return_value=_mock_response(_NSE_XML)), \
         patch("src.ingestion.exchange_rss.is_valid_nse_symbol", return_value=False), \
         patch("src.ingestion.exchange_rss.resolve_nse_symbol", return_value="RELIANCE") as mock_resolve:
        articles = exchange_rss.fetch_nse_rss(hours_back=36)

    assert len(articles) == 1
    assert articles[0].ticker == "RELIANCE.NS"
    mock_resolve.assert_called_once_with("Reliance Industries Limited")


def test_nse_rss_keeps_prefix_heuristic_when_symbol_master_unavailable():
    # Regression: if the symbol master can't load (NSE archives are flaky and
    # timed out repeatedly in practice), is_valid_nse_symbol() returns False for
    # EVERY symbol. Naively treating that as "bad symbol" would downgrade a
    # perfectly good RELIANCE.NS to the unpriceable string "Reliance Industries
    # Limited" — breaking the outcome tracking this whole module exists to fix.
    # With no master to validate against, keep the old prefix heuristic.
    with patch("src.ingestion.exchange_rss.requests.get", return_value=_mock_response(_NSE_XML)), \
         patch("src.ingestion.exchange_rss.is_valid_nse_symbol", return_value=False), \
         patch("src.ingestion.exchange_rss.resolve_nse_symbol", return_value=None), \
         patch("src.ingestion.exchange_rss.is_master_available", return_value=False):
        articles = exchange_rss.fetch_nse_rss(hours_back=36)

    assert len(articles) == 1
    assert articles[0].ticker == "RELIANCE.NS"  # NOT the company name


def test_nse_rss_unresolvable_ticker_keeps_company_name():
    # Master IS available (so validation is meaningful) but the company genuinely
    # isn't main-board NSE-listed (e.g. an SME/Emerge listing) — the alert must
    # still go out under its company name (unpriced) rather than being dropped.
    with patch("src.ingestion.exchange_rss.requests.get", return_value=_mock_response(_NSE_XML)), \
         patch("src.ingestion.exchange_rss.is_valid_nse_symbol", return_value=False), \
         patch("src.ingestion.exchange_rss.resolve_nse_symbol", return_value=None), \
         patch("src.ingestion.exchange_rss.is_master_available", return_value=True):
        articles = exchange_rss.fetch_nse_rss(hours_back=36)

    assert len(articles) == 1
    assert articles[0].ticker == "Reliance Industries Limited"


def test_nse_rss_fetch_failure_returns_empty_list():
    with patch("src.ingestion.exchange_rss.requests.get", side_effect=ConnectionError("down")):
        assert exchange_rss.fetch_nse_rss() == []


def test_malformed_xml_fails_soft():
    with patch("src.ingestion.exchange_rss.requests.get", return_value=_mock_response("not xml")):
        assert exchange_rss.fetch_bse_rss() == []
        assert exchange_rss.fetch_nse_rss() == []

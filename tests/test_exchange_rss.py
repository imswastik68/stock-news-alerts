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
    with patch("src.ingestion.exchange_rss.requests.get", return_value=_mock_response(_BSE_XML)):
        articles = exchange_rss.fetch_bse_rss(hours_back=36)

    assert len(articles) == 1  # the 100h-old item is dropped
    a = articles[0]
    assert a.ticker == "543254.BO"
    assert a.source == "bse_rss"
    assert "dividend" in a.headline.lower()
    assert a.attachment_url.endswith(".pdf")


def test_bse_rss_fetch_failure_returns_empty_list():
    with patch("src.ingestion.exchange_rss.requests.get", side_effect=ConnectionError("down")):
        assert exchange_rss.fetch_bse_rss() == []


def test_nse_rss_extracts_category_from_subject_and_ticker_from_filename():
    with patch("src.ingestion.exchange_rss.requests.get", return_value=_mock_response(_NSE_XML)):
        articles = exchange_rss.fetch_nse_rss(hours_back=36)

    assert len(articles) == 1
    a = articles[0]
    assert a.ticker == "RELIANCE.NS"
    assert a.source == "nse_announcements"  # same source class as the date-range API
    assert a.category == "Awarding of order(s)/contract(s)"
    assert "order win" in a.headline.lower()


def test_nse_rss_fetch_failure_returns_empty_list():
    with patch("src.ingestion.exchange_rss.requests.get", side_effect=ConnectionError("down")):
        assert exchange_rss.fetch_nse_rss() == []


def test_malformed_xml_fails_soft():
    with patch("src.ingestion.exchange_rss.requests.get", return_value=_mock_response("not xml")):
        assert exchange_rss.fetch_bse_rss() == []
        assert exchange_rss.fetch_nse_rss() == []

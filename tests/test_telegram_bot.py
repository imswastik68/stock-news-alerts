"""Tests for the Telegram alert formatter — specifically the BSE-ticker display
fix. BSE tickers like '532933.BO' are indistinguishable from a domain name
ending in Bolivia's real '.bo' ccTLD; Telegram auto-linkifies them, which splits
the alert's single bold title into two adjacent bold spans (confirmed live: the
raw entities showed a MessageEntityUrl + two MessageEntityBold spans instead of
one, rendering as a garbled '****' and turning the ticker into a stray link)."""

from __future__ import annotations

from datetime import datetime, timezone

from src.alerting.telegram_bot import _display_ticker, _format_alert
from src.storage.models import Article


def _article(ticker: str) -> Article:
    return Article(
        ticker=ticker,
        headline="Test headline",
        url="https://example.com/x",
        source="nse_announcements",
        published_at=datetime.now(timezone.utc),
        category="Financial Results",
        impact_tier="high",
        event_type="earnings_surprise",
        direction="bullish",
        confidence=0.8,
        materiality_score=0.8,
        impact_horizon="1_3_days",
        reasoning="test",
    )


def test_bse_ticker_display_strips_dotted_bo_suffix():
    # ".BO" looks like Bolivia's ccTLD to Telegram's link auto-detector — must
    # not appear as a dotted suffix in the rendered alert text.
    assert _display_ticker("532933.BO") == "532933 (BSE)"


def test_nse_ticker_display_unchanged():
    assert _display_ticker("RELIANCE.NS") == "RELIANCE.NS"


def test_format_alert_bse_title_has_no_dotted_bo():
    text = _format_alert(_article("543254.BO"))
    title_line = text.splitlines()[0]
    assert ".BO" not in title_line
    assert "543254 (BSE)" in title_line


def test_format_alert_nse_title_unaffected():
    text = _format_alert(_article("RELIANCE.NS"))
    title_line = text.splitlines()[0]
    assert "RELIANCE.NS" in title_line

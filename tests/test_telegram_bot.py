"""Tests for the Telegram alert formatter — specifically the BSE-ticker display
fix. BSE tickers like '532933.BO' are indistinguishable from a domain name
ending in Bolivia's real '.bo' ccTLD; Telegram auto-linkifies them, which splits
the alert's single bold title into two adjacent bold spans (confirmed live: the
raw entities showed a MessageEntityUrl + two MessageEntityBold spans instead of
one, rendering as a garbled '****' and turning the ticker into a stray link)."""

from __future__ import annotations

import math
from datetime import datetime, timezone

from src.alerting.telegram_bot import _display_ticker, _format_alert, _format_price_line
from src.storage.models import Article


def _article(ticker: str, company_name: str | None = None) -> Article:
    return Article(
        ticker=ticker,
        company_name=company_name,
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


# ── BSE company-name display ─────────────────────────────────────────────────
# A BSE-only scrip code ('532933.BO') is a meaningless number to a human. When
# the company name is known it must lead the title instead — the raw number is
# what actually went out for 11 alerts and prompted this fix.

def test_bse_ticker_display_uses_company_name_when_available():
    assert _display_ticker("539016.BO", "Aurum PropTech") == "Aurum PropTech (BSE)"


def test_bse_ticker_display_falls_back_to_code_without_company_name():
    assert _display_ticker("539016.BO", None) == "539016 (BSE)"
    assert _display_ticker("539016.BO", "") == "539016 (BSE)"


def test_nse_ticker_display_ignores_company_name():
    # Dual-listed names already resolve to a real NSE ticker; keep showing it.
    assert _display_ticker("RELIANCE.NS", "Reliance Industries") == "RELIANCE.NS"


def test_format_alert_bse_title_shows_company_name_not_number():
    text = _format_alert(_article("539016.BO", company_name="Aurum PropTech"))
    title_line = text.splitlines()[0]
    assert "Aurum PropTech (BSE)" in title_line
    assert "539016" not in title_line
    assert ".BO" not in title_line


# ── NaN price-line guard ─────────────────────────────────────────────────────
# Regression: yfinance can return NaN for the latest bar early in the trading
# session. `is None` doesn't catch it (NaN is a valid float, not None) and NaN
# is truthy in Python (`if vol:` would pass it through). Confirmed live: two
# real alerts on 2026-07-14 read literal "₹nan | ▼nan%" in the price line.
# get_quote() now filters NaN at the source (tests/test_market_data.py), but
# _format_price_line takes `quote` as a plain dict from any caller, so it
# guards independently too — these tests hand-construct a NaN quote directly,
# bypassing get_quote entirely, to prove this layer holds on its own.

def test_price_line_nan_price_is_suppressed_entirely():
    assert _format_price_line({"price": float("nan"), "pct_change": 1.0, "avg_volume": 1000}) is None


def test_price_line_nan_pct_change_is_dropped_not_leaked():
    line = _format_price_line({"price": 100.0, "pct_change": float("nan"), "avg_volume": 1000})
    assert line is not None
    assert "nan" not in line.lower()
    assert "₹100.00" in line


def test_price_line_nan_volume_is_dropped_not_leaked():
    line = _format_price_line({"price": 100.0, "pct_change": 2.0, "avg_volume": float("nan")})
    assert line is not None
    assert "nan" not in line.lower()


def test_price_line_none_quote_returns_none():
    assert _format_price_line(None) is None


def test_price_line_normal_quote_unaffected():
    line = _format_price_line({"price": 1670.30, "pct_change": 3.6, "avg_volume": 1_700_000})
    assert line == "💹 ₹1,670.30 | ▲3.6% | vol 1.7M"

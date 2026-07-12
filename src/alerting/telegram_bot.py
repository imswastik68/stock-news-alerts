"""
Telegram alerting — same stack as stock_selector's src/telegram_alert.py:
python-telegram-bot's async `telegram.Bot`, HTML parse mode, html.escape on all
dynamic text, and asyncio.run() to call the async send from sync pipeline code.
Same env var names (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID) so credentials can be
copied straight from an existing stock_selector .env if desired.
"""

from __future__ import annotations

import asyncio
import html
import logging

import telegram

from src.config import get_settings
from src.storage.models import Article

logger = logging.getLogger(__name__)

_DIRECTION_EMOJI = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}


def _e(text: str) -> str:
    return html.escape(str(text))


def _event_type_label(event_type: str) -> str:
    return event_type.replace("_", " ").title()


def _format_price_line(quote: dict | None) -> str | None:
    """Price + %move + liquidity context, so you can tell a real mover / liquid
    name from an illiquid micro-cap. Skipped if the quote couldn't be fetched."""
    if not quote or quote.get("price") is None:
        return None
    price = quote["price"]
    pct = quote.get("pct_change")
    vol = quote.get("avg_volume")
    parts = [f"₹{price:,.2f}"]
    if pct is not None:
        arrow = "▲" if pct >= 0 else "▼"
        parts.append(f"{arrow}{abs(pct):.1f}%")
    if vol:
        parts.append(f"vol {vol/1e6:.1f}M" if vol >= 1e6 else f"vol {vol/1e3:.0f}K")
    return "💹 " + " | ".join(parts)


def _format_alert(article: Article, quote: dict | None = None) -> str:
    emoji = _DIRECTION_EMOJI.get(article.direction, "⚪")
    event_label = _event_type_label(article.event_type)
    direction_label = article.direction.capitalize()
    confidence_pct = round(article.confidence * 100)

    lines = [
        f"{emoji} <b>{_e(article.ticker)} — {_e(event_label)} ({_e(direction_label)})</b>",
    ]
    # Show the official exchange category for filings — this is the "why it's
    # high-impact" signal, like the pro platforms' tags.
    if article.category:
        tier = f" [{article.impact_tier.upper()}]" if article.impact_tier else ""
        lines.append(f"📋 {_e(article.category)}{_e(tier)}")
    price_line = _format_price_line(quote)
    if price_line:
        lines.append(_e(price_line))
    lines += [
        f"Confidence: {confidence_pct}%",
        f"Materiality: {round(article.materiality_score * 100)}% | Horizon: {_e(article.impact_horizon)}",
        f'"{_e(article.headline)}"',
        f"Reason: {_e(article.reasoning)}",
        f'🔗 <a href="{_e(article.url)}">Read more</a>',
    ]
    return "\n".join(lines)


async def _send(token: str, chat_id: str, text: str) -> None:
    bot = telegram.Bot(token=token)
    await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")


def _dispatch(text: str) -> bool:
    settings = get_settings()
    if settings.dry_run:
        logger.info("telegram (DRY_RUN): %s", text.replace("\n", " | "))
        return False

    if not settings.telegram_token or not settings.telegram_chat_id:
        logger.warning("telegram: TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set — skipping")
        return False

    try:
        asyncio.run(_send(settings.telegram_token, settings.telegram_chat_id, text))
        return True
    except Exception as exc:
        logger.error("telegram: send failed: %s", exc)
        return False


def send_alert(article: Article, quote: dict | None = None) -> bool:
    """Send a high-confidence alert for one article. Returns True on success —
    caller (pipeline.py) marks alert_sent only when this returns True, so a
    failed send is retried on the next cycle instead of silently lost."""
    return _dispatch(_format_alert(article, quote))


def send_startup_message() -> bool:
    return _dispatch("🟢 <b>stock-news-alerts</b> is online and monitoring NSE filings.")


def send_daily_summary(processed: int, alerts_sent: int) -> bool:
    text = (
        f"📊 <b>Daily summary</b>\n"
        f"Today: {processed} article(s) processed, {alerts_sent} high-confidence alert(s) sent."
    )
    return _dispatch(text)


if __name__ == "__main__":
    settings = get_settings()
    logging.basicConfig(level=logging.INFO)
    if settings.dry_run:
        print("DRY_RUN is true — set DRY_RUN=false in .env to actually send the test message.")
    ok = _dispatch("👋 Hello from stock-news-alerts! Telegram integration is working.")
    print("Smoke test succeeded." if ok else "Smoke test FAILED — check TELEGRAM_TOKEN/TELEGRAM_CHAT_ID.")

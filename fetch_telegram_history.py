"""
Fetch the messages this project's bot has sent to your Telegram chat, so you can
read back / verify the alerts that were delivered.

Uses Telethon with a USER session (not the bot API — bots can't read their own
history). The bot's numeric entity id is derived from TELEGRAM_TOKEN (the part
before the ':' IS the bot's user id), so nothing extra to configure.

Requirements:
  - TG_API_ID / TG_API_HASH in .env (create at https://my.telegram.org → API
    development tools). You can reuse the same values as stock_selector.
  - First run is INTERACTIVE: Telethon asks for your phone number and the login
    code Telegram sends you, then saves a session file (tg_session.session) so
    later runs are non-interactive.

Run: python fetch_telegram_history.py
Re-running is safe — it fetches incrementally and appends only new messages.
Output: telegram_history.json
"""

from __future__ import annotations

import json
import sys
from datetime import timezone
from pathlib import Path

import requests

try:
    from telethon import TelegramClient
except ImportError:
    sys.exit("Run: nenv/bin/pip install telethon")

from src.config import get_settings

_ROOT = Path(__file__).resolve().parent
_SESSION = _ROOT / "tg_session"
_HISTORY_OUT = _ROOT / "telegram_history.json"


def _bot_entity_id(telegram_token: str) -> int:
    """The bot's user id is the integer before ':' in its token."""
    head = telegram_token.split(":", 1)[0]
    return int(head)


async def _resolve_bot(client: "TelegramClient", telegram_token: str, telegram_username: str | None):
    """Telethon can't resolve an entity from a bare numeric id unless it's
    already cached in this session (raises 'Could not find the input entity').
    Resolving by @username works unconditionally via contacts.resolveUsername,
    so prefer that; fall back to the numeric id (works once the bot has been
    seen at least once through this session)."""
    if telegram_username:
        return await client.get_entity(telegram_username.lstrip("@"))
    return await client.get_entity(_bot_entity_id(telegram_token))


def _load_existing() -> list[dict]:
    if _HISTORY_OUT.exists():
        return json.loads(_HISTORY_OUT.read_text())
    return []


async def fetch() -> None:
    settings = get_settings()
    if not settings.tg_api_id or not settings.tg_api_hash:
        sys.exit("TG_API_ID / TG_API_HASH missing from .env (get them at https://my.telegram.org).")
    if not settings.telegram_token:
        sys.exit("TELEGRAM_TOKEN missing from .env — needed to identify the bot.")

    history = _load_existing()
    seen_ids = {m["id"] for m in history}

    # Bot API getMe (no login needed) gives the @username, which Telethon can
    # resolve reliably; the raw numeric id alone fails unless already cached in
    # this session (see _resolve_bot).
    username = None
    try:
        me = requests.get(
            f"https://api.telegram.org/bot{settings.telegram_token}/getMe", timeout=15
        ).json()
        username = me.get("result", {}).get("username")
    except Exception:
        pass

    client = TelegramClient(str(_SESSION), settings.tg_api_id, settings.tg_api_hash)
    await client.start()
    bot_entity = await _resolve_bot(client, settings.telegram_token, username)

    new_msgs: list[dict] = []
    async for msg in client.iter_messages(bot_entity, limit=None):
        if not msg.text or msg.id in seen_ids:
            continue
        new_msgs.append(
            {
                "id": msg.id,
                "date": msg.date.astimezone(timezone.utc).isoformat(),
                "text": msg.text,
            }
        )

    await client.disconnect()

    if not new_msgs:
        print(f"No new messages. ({len(history)} already stored)")
        return

    history.extend(new_msgs)
    history.sort(key=lambda m: m["date"])
    _HISTORY_OUT.write_text(json.dumps(history, indent=2, ensure_ascii=False))
    print(f"Fetched {len(new_msgs)} new message(s) ({len(history)} total) -> {_HISTORY_OUT.name}")

    print("\nMost recent alerts:")
    for m in history[-5:]:
        first_line = m["text"].splitlines()[0] if m["text"] else ""
        print(f"  {m['date'][:16]}  {first_line[:70]}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(fetch())

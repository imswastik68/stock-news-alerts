"""Single settings loader: .env + watchlist.yaml + confidence_table.yaml, plus logging setup."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent

load_dotenv(ROOT_DIR / ".env")


def _bool_env(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class WatchlistEntry:
    ticker: str
    name: str
    bse_code: str | None = None


@dataclass
class Settings:
    # LLM backend
    inference_backend: str
    groq_api_key: str
    ollama_url: str
    ollama_model: str

    # Telegram
    telegram_token: str
    telegram_chat_id: str
    tg_api_id: int
    tg_api_hash: str

    # NewsAPI
    newsapi_api_key: str
    newsapi_daily_cap: int

    # Pipeline
    coverage_mode: str  # "market_wide" (all NSE filings) or "watchlist" (your tickers + media)
    poll_interval_minutes: int
    alert_confidence_threshold: float
    min_materiality_score: float
    min_source_quality_for_alerts: float
    max_articles_per_cycle: int
    max_google_news_per_ticker: int
    max_news_age_hours: int
    daily_summary_enabled: bool
    daily_summary_hour: int

    # Storage
    db_path: str

    # Misc
    dry_run: bool
    log_level: str

    # Loaded from yaml
    watchlist: list[WatchlistEntry] = field(default_factory=list)
    confidence_base_rates: dict[str, float] = field(default_factory=dict)


def _load_watchlist() -> list[WatchlistEntry]:
    path = ROOT_DIR / "watchlist.yaml"
    # Optional: market_wide mode ignores the watchlist, and the file is gitignored
    # (not present on a fresh clone / CI checkout). Absent = empty watchlist.
    if not path.exists():
        return []
    with open(path) as f:
        raw = yaml.safe_load(f) or []
    return [
        WatchlistEntry(
            ticker=e["ticker"],
            name=e["name"],
            bse_code=str(e["bse_code"]) if e.get("bse_code") else None,
        )
        for e in raw
    ]


def _load_confidence_table() -> tuple[dict[str, float], float]:
    path = ROOT_DIR / "confidence_table.yaml"
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    base_rates = raw.get("base_rates", {})
    yaml_threshold = float(raw.get("alert_threshold", 0.70))
    return base_rates, yaml_threshold


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is not None:
        return _settings

    base_rates, yaml_threshold = _load_confidence_table()
    env_threshold = os.environ.get("ALERT_CONFIDENCE_THRESHOLD")
    threshold = float(env_threshold) if env_threshold else yaml_threshold

    _settings = Settings(
        inference_backend=os.environ.get("INFERENCE_BACKEND", "groq").lower(),
        groq_api_key=os.environ.get("GROQ_API_KEY", ""),
        ollama_url=os.environ.get("OLLAMA_URL", "http://localhost:11434/v1"),
        ollama_model=os.environ.get("OLLAMA_MODEL", "qwen3:8b"),
        telegram_token=os.environ.get("TELEGRAM_TOKEN", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        tg_api_id=int(os.environ.get("TG_API_ID", "0") or "0"),
        tg_api_hash=os.environ.get("TG_API_HASH", ""),
        newsapi_api_key=os.environ.get("NEWSAPI_API_KEY", ""),
        newsapi_daily_cap=int(os.environ.get("NEWSAPI_DAILY_CAP", "90")),
        coverage_mode=os.environ.get("COVERAGE_MODE", "market_wide").lower(),
        poll_interval_minutes=int(os.environ.get("POLL_INTERVAL_MINUTES", "2")),
        alert_confidence_threshold=threshold,
        min_materiality_score=float(os.environ.get("MIN_MATERIALITY_SCORE", "0.65")),
        min_source_quality_for_alerts=float(os.environ.get("MIN_SOURCE_QUALITY_FOR_ALERTS", "0.55")),
        max_articles_per_cycle=int(os.environ.get("MAX_ARTICLES_PER_CYCLE", "10")),
        max_google_news_per_ticker=int(os.environ.get("MAX_GOOGLE_NEWS_PER_TICKER", "8")),
        max_news_age_hours=int(os.environ.get("MAX_NEWS_AGE_HOURS", "48")),
        daily_summary_enabled=_bool_env("DAILY_SUMMARY_ENABLED", True),
        daily_summary_hour=int(os.environ.get("DAILY_SUMMARY_HOUR", "18")),
        db_path=os.environ.get("DB_PATH", "stock_news.db"),
        dry_run=_bool_env("DRY_RUN", False),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        watchlist=_load_watchlist(),
        confidence_base_rates=base_rates,
    )
    return _settings


def configure_logging() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Avoid leaking full API URLs in logs. Telegram Bot API URLs contain the bot
    # token, and httpx logs request URLs at INFO level.
    logging.getLogger("httpx").setLevel(logging.WARNING)

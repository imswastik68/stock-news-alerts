"""Cheap pre-LLM materiality filter.

The LLM is useful for nuanced classification, but it should only see articles
that look like they might contain fresh, company-specific, stock-moving facts.
This filter keeps broad Google/NewsAPI noise from burning free-tier LLM calls.
"""

from __future__ import annotations

from src.ingestion.common import RawArticle

OFFICIAL_SOURCES = {"nse_announcements", "bse_announcements"}

_MATERIAL_KEYWORDS = {
    "acquisition",
    "approved",
    "bagged",
    "bankruptcy",
    "board",
    "bonus",
    "buyback",
    "capex",
    "ceo",
    "cfo",
    "contract",
    "credit rating",
    "default",
    "demerger",
    "dividend",
    "downgrade",
    "earnings",
    "ebitda",
    "fined",
    "fraud",
    "guidance",
    "insider",
    "investigation",
    "ipo",
    "joint venture",
    "lawsuit",
    "litigation",
    "merger",
    "order",
    "penalty",
    "pledge",
    "profit",
    "promoter",
    "q1",
    "q2",
    "q3",
    "q4",
    "quarter",
    "rating",
    "record date",
    "regulator",
    "results",
    "revenue",
    "rights issue",
    "sebi",
    "stake",
    "stake sale",
    "split",
    "tariff",
    "upgrade",
}

# Reliable listicle / broker-note markers — low value even when the headline
# also contains a material-sounding word. Kept deliberately NARROW: broad index
# names ("nifty", "sensex") and "market today" were removed because they appear
# constantly in genuine company-news headlines ("TCS Q1 profit beats, Sensex
# rallies"). Vetoing on those silently dropped real, alertable news — and since
# prefilter-skipped articles are stored permanently as `other`, they never get
# re-classified. A material keyword must be able to win over an index name.
_NOISE_PHRASES = {
    "stocks to buy",
    "share price target",
    "target price",
    "technical analysis",
    "top gainers",
    "top losers",
}


def should_use_llm(article: RawArticle) -> bool:
    if article.source in OFFICIAL_SOURCES:
        return True

    text = f"{article.headline} {article.summary}".lower()
    if any(phrase in text for phrase in _NOISE_PHRASES):
        return False
    return any(keyword in text for keyword in _MATERIAL_KEYWORDS)

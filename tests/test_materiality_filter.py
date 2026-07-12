from __future__ import annotations

from datetime import datetime, timezone

from src.ingestion.common import RawArticle
from src.scoring.materiality_filter import should_use_llm


def _article(headline: str, source: str = "google_news") -> RawArticle:
    return RawArticle(
        ticker="RELIANCE.NS",
        headline=headline,
        summary="",
        url="https://example.com",
        source=source,
        published_at=datetime.now(timezone.utc),
    )


def test_official_sources_always_use_llm():
    assert should_use_llm(_article("Routine filing", source="nse_announcements"))
    assert should_use_llm(_article("Routine filing", source="bse_announcements"))


def test_material_company_news_uses_llm():
    assert should_use_llm(_article("Reliance board approves buyback and dividend"))
    assert should_use_llm(_article("Infosys Q1 profit beats estimates"))


def test_generic_market_noise_skips_llm():
    assert not should_use_llm(_article("Stocks to buy: Reliance among five short-term picks"))
    assert not should_use_llm(_article("Nifty and Sensex market today: Reliance shares in focus"))


def test_material_news_mentioning_index_still_uses_llm():
    # Regression: a material keyword must win over a broad index name in the same
    # headline — these were previously vetoed by the noise gate and lost.
    assert should_use_llm(_article("Reliance Q1 results: net profit beats estimates, Sensex rallies"))
    assert should_use_llm(_article("Infosys wins $2 billion order; Nifty IT index jumps"))
    assert should_use_llm(_article("HDFC Bank Q2 profit rises 18% as Nifty Bank hits record"))

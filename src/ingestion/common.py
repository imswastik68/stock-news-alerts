"""Shared types for news ingestion sources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class RawArticle:
    ticker: str
    headline: str
    summary: str
    url: str
    source: str
    published_at: datetime
    # Exchange announcement category (NSE `desc` / BSE category). Empty for media
    # sources. Drives the pre-LLM impact-tier filter — see src/scoring/impact.py.
    category: str = ""
    # Text extracted from the filing's PDF attachment (the actual content: results
    # numbers, order value, rating…). Empty until fetched / if unextractable.
    body: str = ""
    # Link to the source PDF, kept separate from `url` (which is the dedup key).
    attachment_url: str = ""

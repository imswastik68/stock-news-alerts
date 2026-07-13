"""
NSE equity symbol master — free company-name -> ticker resolution.

Two real gaps this closes:
  1. BSE-only-priced alerts (e.g. `544574.BO`) for companies that are ALSO
     listed on NSE (e.g. Tata Capital = TATACAP.NS) have zero Yahoo Finance
     price data under their BSE scrip, permanently breaking outcome tracking
     for them. Resolving to the NSE ticker fixes pricing/outcomes/dedup.
  2. NSE RSS items have no structured symbol field; the PDF-filename-prefix
     heuristic in exchange_rss.py sometimes yields an invalid or wrong symbol
     (e.g. "AWHCLP" instead of "AWHCL"). This validates/corrects it against the
     real symbol list, falling back to a company-name lookup.

Source: NSE's own published equity list (no auth, no key):
  https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv

Matching is STRICT normalized-full-name-equality only — never fuzzy/substring.
"Damodar Valley Corporation" (a PSU, not NSE-listed) must NOT match "Damodar
Industries Limited" just because they share a word; a fuzzy matcher would get
this wrong, so it isn't used.

Fails soft: any network/parse problem returns an empty master, and every public
function degrades to "unresolved" (None / False) rather than raising — matches
the project-wide contract that ingestion never crashes the pipeline.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import time

import requests

from src.config import ROOT_DIR

logger = logging.getLogger(__name__)

_MASTER_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
_TIMEOUT = 20

_CACHE_PATH = ROOT_DIR / ".symbol_master.json"
_CACHE_TTL_SECONDS = 24 * 60 * 60  # 24h — the equity list changes rarely

# In-memory memoization so repeated calls within one process (or a chatty
# pipeline cycle) don't even touch disk after the first resolution.
_name_to_symbol: dict[str, str] | None = None
_valid_symbols: set[str] | None = None

_ABBREV_EXPANSIONS = [
    (r"\bLTD\b", "LIMITED"),
    (r"\bPVT\b", "PRIVATE"),
    (r"\bCO\b", "COMPANY"),
    (r"\bCORP\b", "CORPORATION"),
    (r"\bIND\b", "INDUSTRIES"),
]


def _normalize(name: str) -> str:
    """Uppercase, expand common abbreviations, strip punctuation, collapse
    whitespace — applied identically to CSV company names and query names so
    'Antony Waste Handling Cell Ltd' matches 'Antony Waste Handling Cell
    Limited' in the master, without any fuzzy/partial matching."""
    text = (name or "").upper()
    text = text.replace("&", " AND ")
    text = re.sub(r"[.,\-']", " ", text)
    for pattern, expansion in _ABBREV_EXPANSIONS:
        text = re.sub(pattern, expansion, text)
    return " ".join(text.split())


def _read_cache() -> dict[str, str] | None:
    if not _CACHE_PATH.exists():
        return None
    try:
        if time.time() - _CACHE_PATH.stat().st_mtime > _CACHE_TTL_SECONDS:
            return None
        return json.loads(_CACHE_PATH.read_text())
    except Exception as exc:
        logger.debug("symbol_master: cache read failed: %s", exc)
        return None


def _write_cache(mapping: dict[str, str]) -> None:
    try:
        _CACHE_PATH.write_text(json.dumps(mapping))
    except Exception as exc:
        logger.debug("symbol_master: cache write failed: %s", exc)


def _download_master() -> dict[str, str]:
    try:
        resp = requests.get(_MASTER_URL, headers=_UA, timeout=_TIMEOUT)
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        mapping: dict[str, str] = {}
        for row in reader:
            symbol = (row.get("SYMBOL") or "").strip()
            company = row.get("NAME OF COMPANY") or ""
            if not symbol or not company:
                continue
            mapping[_normalize(company)] = symbol
        return mapping
    except Exception as exc:
        logger.warning("symbol_master: download/parse failed: %s", exc)
        return {}


def _load_master() -> dict[str, str]:
    """Normalized-company-name -> SYMBOL, memoized in-process then on disk
    (24h TTL). Returns {} on any failure — never raises."""
    global _name_to_symbol, _valid_symbols
    if _name_to_symbol is not None:
        return _name_to_symbol

    cached = _read_cache()
    mapping = cached if cached is not None else _download_master()
    if cached is None and mapping:
        _write_cache(mapping)

    _name_to_symbol = mapping
    _valid_symbols = set(mapping.values())
    return _name_to_symbol


def resolve_nse_symbol(company_name: str) -> str | None:
    """Strict normalized-name lookup. Returns the NSE SYMBOL or None if the
    name isn't found (including if the master itself failed to load)."""
    if not company_name or not company_name.strip():
        return None
    master = _load_master()
    return master.get(_normalize(company_name))


def is_valid_nse_symbol(symbol: str) -> bool:
    """True if `symbol` is a real NSE-listed equity symbol. Used to validate
    (not just guess) a PDF-filename-derived symbol prefix."""
    if not symbol:
        return False
    _load_master()
    return bool(_valid_symbols) and symbol.strip().upper() in _valid_symbols

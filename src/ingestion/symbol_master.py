"""
NSE equity symbol master — free company-name -> ticker resolution.

Three real gaps this closes:
  1. BSE-only-priced alerts (e.g. `544574.BO`) for companies that are ALSO
     listed on NSE (e.g. Tata Capital = TATACAP.NS) have zero Yahoo Finance
     price data under their BSE scrip, permanently breaking outcome tracking
     for them. Resolving to the NSE ticker fixes pricing/outcomes/dedup.
  2. NSE RSS items have no structured symbol field; the PDF-filename-prefix
     heuristic in exchange_rss.py sometimes yields an invalid or wrong symbol
     (e.g. "AWHCLP" instead of "AWHCL"). This validates/corrects it against the
     real symbol list, falling back to a company-name lookup.
  3. Some alerted companies are SME/Emerge-board listings absent from the
     main-board list entirely (e.g. Chavda Infra, Transteel Seating) — these
     showed up as "no ticker at all" (raw company name, unpriced, un-deduped).
     Merging NSE's separate SME/Emerge list resolves them too.

Sources: NSE's own published equity lists (no auth, no key):
  main board: https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv
  SME/Emerge: https://nsearchives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv
Note the company-name COLUMN differs between the two: main board uses
"NAME OF COMPANY" (spaces), SME uses "NAME_OF_COMPANY" (underscore) — verified
live 2026-07-14. Both are tried per row so either header works.

Matching is STRICT normalized-full-name-equality only — never fuzzy/substring.
"Damodar Valley Corporation" (a PSU, not NSE-listed) must NOT match "Damodar
Industries Limited" just because they share a word; a fuzzy matcher would get
this wrong, so it isn't used.

On a name collision between the two lists, the main board wins (it's the
larger, more liquid, more likely-correct listing for that exact name).

Fails soft, per source independently: a failure fetching the SME list does not
prevent main-board resolution (and vice versa). Every public function degrades
to "unresolved" (None / False) rather than raising if BOTH fail — matches the
project-wide contract that ingestion never crashes the pipeline.
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
_SME_MASTER_URL = "https://nsearchives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv"
_NAME_COLUMNS = ("NAME OF COMPANY", "NAME_OF_COMPANY")
# Same header-naming split as the company-name column, verified live 2026-07-22:
# the main board writes " ISIN NUMBER" (note the LEADING SPACE), SME writes
# "ISIN_NUMBER". All spellings are tried per row so either file works.
_ISIN_COLUMNS = (" ISIN NUMBER", "ISIN NUMBER", "ISIN_NUMBER")
_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
_TIMEOUT = 20

_CACHE_PATH = ROOT_DIR / ".symbol_master.json"
_ISIN_CACHE_PATH = ROOT_DIR / ".symbol_master_isin.json"
_CACHE_TTL_SECONDS = 24 * 60 * 60  # 24h — the equity list changes rarely

# In-memory memoization so repeated calls within one process (or a chatty
# pipeline cycle) don't even touch disk after the first resolution.
_name_to_symbol: dict[str, str] | None = None
_valid_symbols: set[str] | None = None
_isin_to_symbol: dict[str, str] | None = None

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


def _company_name(row: dict) -> str:
    for col in _NAME_COLUMNS:
        val = row.get(col)
        if val:
            return val
    return ""


def _download_csv_mapping(url: str, source_label: str) -> dict[str, str]:
    """Fetch one NSE symbol-list CSV -> {normalized company name: SYMBOL}.
    Fails soft: any network/parse problem logs and returns {}."""
    try:
        resp = requests.get(url, headers=_UA, timeout=_TIMEOUT)
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        mapping: dict[str, str] = {}
        for row in reader:
            symbol = (row.get("SYMBOL") or "").strip()
            company = _company_name(row)
            if not symbol or not company:
                continue
            mapping[_normalize(company)] = symbol
        return mapping
    except Exception as exc:
        logger.warning("symbol_master: %s download/parse failed: %s", source_label, exc)
        return {}


def _download_master() -> dict[str, str]:
    """Main-board + SME/Emerge symbol lists, merged (main board wins on a name
    collision). Each source fails independently — one being unreachable never
    blocks the other."""
    sme = _download_csv_mapping(_SME_MASTER_URL, "SME/Emerge")
    main = _download_csv_mapping(_MASTER_URL, "main board")
    return {**sme, **main}


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


def _download_isin_mapping(url: str, source_label: str) -> dict[str, str]:
    """One NSE list -> {ISIN: SYMBOL}. Fails soft, like its name-based twin."""
    try:
        resp = requests.get(url, headers=_UA, timeout=_TIMEOUT)
        resp.raise_for_status()
        mapping: dict[str, str] = {}
        for row in csv.DictReader(io.StringIO(resp.text)):
            symbol = (row.get("SYMBOL") or "").strip()
            isin = ""
            for col in _ISIN_COLUMNS:
                if row.get(col):
                    isin = row[col].strip().upper()
                    break
            if symbol and isin:
                mapping[isin] = symbol
        return mapping
    except Exception as exc:
        logger.warning("symbol_master: %s ISIN download/parse failed: %s", source_label, exc)
        return {}


def _download_isin_master() -> dict[str, str]:
    sme = _download_isin_mapping(_SME_MASTER_URL, "SME/Emerge")
    main = _download_isin_mapping(_MASTER_URL, "main board")
    return {**sme, **main}


def _load_isin_master() -> dict[str, str]:
    """{ISIN: NSE SYMBOL}, memoized in-process then on disk (24h TTL)."""
    global _isin_to_symbol
    if _isin_to_symbol is not None:
        return _isin_to_symbol

    cached = None
    if _ISIN_CACHE_PATH.exists():
        try:
            if time.time() - _ISIN_CACHE_PATH.stat().st_mtime <= _CACHE_TTL_SECONDS:
                cached = json.loads(_ISIN_CACHE_PATH.read_text())
        except Exception as exc:
            logger.debug("symbol_master: ISIN cache read failed: %s", exc)

    mapping = cached if cached is not None else _download_isin_master()
    if cached is None and mapping:
        try:
            _ISIN_CACHE_PATH.write_text(json.dumps(mapping))
        except Exception as exc:
            logger.debug("symbol_master: ISIN cache write failed: %s", exc)

    _isin_to_symbol = mapping
    return _isin_to_symbol


def resolve_nse_symbol_by_isin(isin: str) -> str | None:
    """ISIN -> NSE SYMBOL. This is the DETERMINISTIC dual-listing resolver and is
    preferred over the company-name path: an ISIN identifies a security exactly,
    so no normalization or fuzzy-matching judgement is involved at all. Measured
    live 2026-07-22: 2214 of 4850 BSE scrips matched an NSE symbol by exact ISIN
    (the remaining 2635 are genuinely BSE-only and correctly resolve to None)."""
    if not isin or not isin.strip():
        return None
    return _load_isin_master().get(isin.strip().upper())


def is_master_available() -> bool:
    """False when the symbol list couldn't be loaded (network down, NSE archives
    flaky — it timed out repeatedly in practice). Callers must NOT treat
    is_valid_nse_symbol()==False as "bad symbol" in that case: with no master to
    check against, *every* symbol looks invalid, which would silently downgrade
    good tickers to unpriceable company names and break outcome tracking — the
    exact failure this module exists to prevent. Fall back to the pre-existing
    heuristic instead."""
    _load_master()
    return bool(_valid_symbols)

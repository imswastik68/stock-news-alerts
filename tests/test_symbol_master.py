"""Tests for the NSE company-name -> ticker resolver.

Matching must be STRICT normalized-full-name equality only — never fuzzy or
substring — because a fuzzy matcher would wrongly resolve "Damodar Valley
Corporation" (a PSU, not NSE-listed) to "Damodar Industries Limited" (NSE:
DAMODARIND) just because they share a word. This is a real case observed in
production RSS data, not a hypothetical.
"""

from __future__ import annotations

from unittest.mock import patch

from src.ingestion import symbol_master

_FAKE_CSV = (
    "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE\n"
    "AWHCL,Antony Waste Handling Cell Limited,EQ,01-JAN-2020,10\n"
    "HGINFRA,H.G. Infra Engineering Limited,EQ,01-JAN-2018,10\n"
    "DAMODARIND,Damodar Industries Limited,EQ,01-JAN-1995,10\n"
    "TATACAP,Tata Capital Limited,EQ,01-JAN-2024,10\n"
)


def _fake_master():
    import csv
    import io

    reader = csv.DictReader(io.StringIO(_FAKE_CSV))
    mapping = {}
    for row in reader:
        symbol = row["SYMBOL"].strip()
        company = row["NAME OF COMPANY"].strip()
        mapping[symbol_master._normalize(company)] = symbol
    return mapping


def setup_function(_):
    # Reset the module-level memoization so each test starts clean.
    symbol_master._name_to_symbol = None
    symbol_master._valid_symbols = None


def test_resolves_with_ltd_to_limited_normalization():
    with patch.object(symbol_master, "_download_master", return_value=_fake_master()), \
         patch.object(symbol_master, "_read_cache", return_value=None), \
         patch.object(symbol_master, "_write_cache"):
        assert symbol_master.resolve_nse_symbol("Antony Waste Handling Cell Ltd") == "AWHCL"


def test_resolves_with_punctuation_normalization():
    with patch.object(symbol_master, "_download_master", return_value=_fake_master()), \
         patch.object(symbol_master, "_read_cache", return_value=None), \
         patch.object(symbol_master, "_write_cache"):
        assert symbol_master.resolve_nse_symbol("H.G. Infra Engineering Limited") == "HGINFRA"


def test_unlisted_psu_does_not_fuzzy_match_similarly_named_company():
    # "Damodar Valley Corporation" is a PSU, not NSE-listed. It must NOT match
    # "Damodar Industries Limited" just because both start with "Damodar".
    with patch.object(symbol_master, "_download_master", return_value=_fake_master()), \
         patch.object(symbol_master, "_read_cache", return_value=None), \
         patch.object(symbol_master, "_write_cache"):
        assert symbol_master.resolve_nse_symbol("Damodar Valley Corporation") is None


def test_download_failure_returns_none_not_exception():
    with patch.object(symbol_master, "_download_master", return_value={}), \
         patch.object(symbol_master, "_read_cache", return_value=None), \
         patch.object(symbol_master, "_write_cache"):
        assert symbol_master.resolve_nse_symbol("Tata Capital Limited") is None


def test_empty_or_none_name_returns_none():
    with patch.object(symbol_master, "_download_master", return_value=_fake_master()):
        assert symbol_master.resolve_nse_symbol("") is None
        assert symbol_master.resolve_nse_symbol(None) is None


def test_is_valid_nse_symbol_true_and_false_cases():
    with patch.object(symbol_master, "_download_master", return_value=_fake_master()), \
         patch.object(symbol_master, "_read_cache", return_value=None), \
         patch.object(symbol_master, "_write_cache"):
        assert symbol_master.is_valid_nse_symbol("AWHCL") is True
        assert symbol_master.is_valid_nse_symbol("awhcl") is True  # case-insensitive
        assert symbol_master.is_valid_nse_symbol("NOTASYMBOL") is False
        assert symbol_master.is_valid_nse_symbol("") is False


def test_is_valid_nse_symbol_false_when_master_unavailable():
    with patch.object(symbol_master, "_download_master", return_value={}), \
         patch.object(symbol_master, "_read_cache", return_value=None), \
         patch.object(symbol_master, "_write_cache"):
        assert symbol_master.is_valid_nse_symbol("AWHCL") is False


def test_disk_cache_used_instead_of_redownloading():
    cached = {"TATA CAPITAL LIMITED": "TATACAP"}
    with patch.object(symbol_master, "_read_cache", return_value=cached), \
         patch.object(symbol_master, "_download_master") as mock_download:
        result = symbol_master.resolve_nse_symbol("Tata Capital Limited")
        assert result == "TATACAP"
        mock_download.assert_not_called()

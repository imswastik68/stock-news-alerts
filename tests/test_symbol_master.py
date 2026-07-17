"""Tests for the NSE company-name -> ticker resolver.

Matching must be STRICT normalized-full-name equality only — never fuzzy or
substring — because a fuzzy matcher would wrongly resolve "Damodar Valley
Corporation" (a PSU, not NSE-listed) to "Damodar Industries Limited" (NSE:
DAMODARIND) just because they share a word. This is a real case observed in
production RSS data, not a hypothetical.
"""

from __future__ import annotations

import requests
from unittest.mock import MagicMock, patch

from src.ingestion import symbol_master

_FAKE_CSV = (
    "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE\n"
    "AWHCL,Antony Waste Handling Cell Limited,EQ,01-JAN-2020,10\n"
    "HGINFRA,H.G. Infra Engineering Limited,EQ,01-JAN-2018,10\n"
    "DAMODARIND,Damodar Industries Limited,EQ,01-JAN-1995,10\n"
    "TATACAP,Tata Capital Limited,EQ,01-JAN-2024,10\n"
)

# SME/Emerge CSV uses NAME_OF_COMPANY (underscore) instead of the main board's
# "NAME OF COMPANY" (spaces) — verified live against NSE's real SME CSV.
_FAKE_SME_CSV = (
    "SYMBOL,NAME_OF_COMPANY,SERIES,DATE_OF_LISTING,PAID_UP_VALUE\n"
    "CHAVDA,Chavda Infra Limited,SM,01-JAN-2023,10\n"
    "TRANSTEEL,Transteel Seating Technologies Limited,SM,01-JAN-2023,10\n"
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


# ── SME/Emerge merge (_download_master / _download_csv_mapping) ─────────────

def _fake_get(url, headers=None, timeout=None):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    if url == symbol_master._SME_MASTER_URL:
        resp.text = _FAKE_SME_CSV
    elif url == symbol_master._MASTER_URL:
        resp.text = _FAKE_CSV
    else:
        raise AssertionError(f"unexpected URL {url}")
    return resp


def test_sme_only_company_name_resolves_via_download_master():
    # A name that exists ONLY in the SME CSV (NAME_OF_COMPANY header) must
    # resolve once merged into the same master mapping.
    with patch.object(symbol_master.requests, "get", side_effect=_fake_get):
        merged = symbol_master._download_master()
    assert merged[symbol_master._normalize("Chavda Infra Limited")] == "CHAVDA"
    assert merged[symbol_master._normalize("Transteel Seating Technologies Limited")] == "TRANSTEEL"


def test_main_board_wins_on_name_collision():
    sme_csv = "SYMBOL,NAME_OF_COMPANY\nSMESYM,Tata Capital Limited\n"

    def fake_get(url, headers=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if url == symbol_master._SME_MASTER_URL:
            resp.text = sme_csv
        else:
            resp.text = _FAKE_CSV  # has TATACAP for the same name
        return resp

    with patch.object(symbol_master.requests, "get", side_effect=fake_get):
        merged = symbol_master._download_master()
    assert merged[symbol_master._normalize("Tata Capital Limited")] == "TATACAP"


def test_sme_fetch_failure_still_leaves_main_board_resolving():
    def fake_get(url, headers=None, timeout=None):
        if url == symbol_master._SME_MASTER_URL:
            raise requests.exceptions.ConnectionError("SME archive unreachable")
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.text = _FAKE_CSV
        return resp

    with patch.object(symbol_master.requests, "get", side_effect=fake_get):
        merged = symbol_master._download_master()
    assert merged[symbol_master._normalize("Tata Capital Limited")] == "TATACAP"
    assert symbol_master._normalize("Chavda Infra Limited") not in merged


def test_main_board_fetch_failure_still_leaves_sme_resolving():
    def fake_get(url, headers=None, timeout=None):
        if url == symbol_master._MASTER_URL:
            raise requests.exceptions.ConnectionError("main board archive unreachable")
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.text = _FAKE_SME_CSV
        return resp

    with patch.object(symbol_master.requests, "get", side_effect=fake_get):
        merged = symbol_master._download_master()
    assert merged[symbol_master._normalize("Chavda Infra Limited")] == "CHAVDA"
    assert symbol_master._normalize("Tata Capital Limited") not in merged


def test_end_to_end_sme_name_resolves_through_resolve_nse_symbol():
    with patch.object(symbol_master.requests, "get", side_effect=_fake_get), \
         patch.object(symbol_master, "_read_cache", return_value=None), \
         patch.object(symbol_master, "_write_cache"):
        assert symbol_master.resolve_nse_symbol("Chavda Infra Limited") == "CHAVDA"
        # main board still works too
        assert symbol_master.resolve_nse_symbol("Tata Capital Limited") == "TATACAP"

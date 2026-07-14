"""Tests for get_quote()'s NaN handling.

Regression: yfinance can return a NaN Close/Volume for the most-recent bar
early in the trading session (the current-day row hasn't fully populated yet).
`is None` doesn't catch NaN (a NaN is a valid float, not None), so an
unfiltered NaN price/pct/volume flowed straight through into a sent Telegram
alert — confirmed live: two real alerts on 2026-07-14 read literal
"₹nan | ▼nan%" instead of a real price line.
"""

from __future__ import annotations

import math
from unittest.mock import MagicMock, patch

import pandas as pd

from src.scoring.market_data import get_quote


def _mock_history(closes, volumes):
    return pd.DataFrame({"Close": closes, "Volume": volumes})


def _patch_yfinance(hist_df):
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = hist_df
    mock_yf = MagicMock()
    mock_yf.Ticker.return_value = mock_ticker
    return patch.dict("sys.modules", {"yfinance": mock_yf})


def test_nan_close_returns_none_not_leaked():
    # The exact bug: the most-recent bar's Close is NaN (incomplete intraday
    # row). Must return None entirely — no usable price at all this call.
    hist = _mock_history([100.0, float("nan")], [1000, 2000])
    with _patch_yfinance(hist):
        assert get_quote("X.NS") is None


def test_nan_prev_close_drops_pct_change_but_keeps_price():
    hist = _mock_history([float("nan"), 105.0], [1000, 2000])
    with _patch_yfinance(hist):
        q = get_quote("X.NS")
    assert q is not None
    assert q["price"] == 105.0
    assert q["pct_change"] is None


def test_all_nan_volume_window_drops_volume_but_keeps_price():
    # pandas .mean() skips individual NaNs by default, so this only triggers
    # when the ENTIRE tail(5) window is NaN (total volume-data unavailability).
    hist = _mock_history([100.0, 109.0], [float("nan"), float("nan")])
    with _patch_yfinance(hist):
        q = get_quote("X.NS")
    assert q is not None
    assert q["price"] == 109.0
    assert q["avg_volume"] is None


def test_single_nan_in_volume_window_is_skipped_by_pandas_mean():
    # A lone NaN among several real volumes does NOT need our guard — pandas
    # already skips it. Documents the boundary the guard above actually covers.
    hist = _mock_history([100.0, 105.0, 106.0, 107.0, 108.0, 109.0], [1000] * 5 + [float("nan")])
    with _patch_yfinance(hist):
        q = get_quote("X.NS")
    assert q is not None
    assert q["avg_volume"] == 1000.0


def test_normal_quote_has_no_nan_anywhere():
    hist = _mock_history([100.0, 105.0], [1000, 2000])
    with _patch_yfinance(hist):
        q = get_quote("X.NS")
    assert q is not None
    for key, val in q.items():
        if val is not None:
            assert not (isinstance(val, float) and math.isnan(val)), f"{key} leaked NaN"

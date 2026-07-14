"""Tests for src/scoring/market_data.py: get_quote()'s NaN handling, and
get_forward_return()'s per-ticker close-price cache.

get_quote regression: yfinance can return a NaN Close/Volume for the most-recent
bar early in the trading session (the current-day row hasn't fully populated
yet). `is None` doesn't catch NaN (a NaN is a valid float, not None), so an
unfiltered NaN price/pct/volume flowed straight through into a sent Telegram
alert — confirmed live: two real alerts on 2026-07-14 read literal
"₹nan | ▼nan%" instead of a real price line.

get_forward_return regression: _closes() cached a per-ticker close-price Series
without accounting for the lookback window actually needed for a given call.
track_outcomes() requests a different window per article depending on its age,
and the same ticker often has multiple alerted articles — a later call needing
a LONGER window than an earlier cached call silently reused the too-short
series. get_forward_return's `next(d >= from_date)` then matched the OLDEST
available (wrong, too-recent) date as the base price instead of failing,
producing a plausible-looking but fabricated forward return with no error.
Reproduced directly: a second call needing a 40-day window got the cached
13-day series and returned a fake number. Fixed by always fetching one fixed,
generous window (comfortably covering the whole valid tracking range) instead
of a caller-supplied variable one, plus an explicit guard that fails closed if
a requested date somehow still predates the cached window.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd

import src.scoring.market_data as market_data
from src.scoring.market_data import get_forward_return, get_quote


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


# ── get_forward_return: per-ticker cache correctness ─────────────────────────

def _closes_series(n_days: int, start_price: float, today: date):
    idx = pd.date_range(end=pd.Timestamp(today), periods=n_days, freq="D")
    return pd.DataFrame({"Close": [start_price + i for i in range(n_days)]}, index=idx)["Close"]


def test_same_ticker_two_calls_always_request_the_same_fixed_window():
    # The bug: a per-ticker cache with a variable requested window meant the
    # SECOND call for the same ticker could silently reuse a too-short series
    # cached by the first. Fixed by always requesting one fixed window — prove
    # both calls request the identical period, so the cache can never be
    # insufficient for a second, differently-aged call.
    today = date.today()
    long_series = _closes_series(60, 200.0, today)
    periods_requested = []

    def fake_ticker(ticker):
        m = MagicMock()

        def history(period):
            periods_requested.append(period)
            return pd.DataFrame({"Close": long_series.values}, index=long_series.index)

        m.history.side_effect = history
        return m

    market_data._close_cache.clear()
    with patch("yfinance.Ticker", side_effect=fake_ticker):
        get_forward_return("X.NS", today - timedelta(days=2), trading_days=1)
        get_forward_return("X.NS", today - timedelta(days=30), trading_days=3)

    assert len(periods_requested) == 1, "second call should hit the cache, not refetch"
    assert periods_requested[0] == f"{market_data._CLOSES_LOOKBACK_DAYS}d"


def test_from_date_older_than_cached_window_returns_none_not_wrong_value():
    # Defense in depth: even if the window were ever insufficient for some
    # other reason, matching the oldest available date as "the" base price
    # would silently fabricate a return. Must fail closed instead.
    today = date.today()
    short_series = _closes_series(20, 100.0, today)
    market_data._close_cache.clear()
    with patch("yfinance.Ticker") as MockTicker:
        MockTicker.return_value.history.return_value = pd.DataFrame(
            {"Close": short_series.values}, index=short_series.index
        )
        result = get_forward_return("Y.NS", today - timedelta(days=90), trading_days=3)
    assert result is None


def test_forward_return_computed_correctly_for_a_normal_case():
    today = date.today()
    series = _closes_series(60, 100.0, today)  # Close = 100, 101, 102, ... at each successive day
    market_data._close_cache.clear()
    with patch("yfinance.Ticker") as MockTicker:
        MockTicker.return_value.history.return_value = pd.DataFrame(
            {"Close": series.values}, index=series.index
        )
        # base is ~10 days ago, target is 3 trading days later -> price rose by 3
        ret = get_forward_return("Z.NS", today - timedelta(days=10), trading_days=3)
    assert ret is not None
    assert ret > 0  # prices are monotonically increasing in the fixture

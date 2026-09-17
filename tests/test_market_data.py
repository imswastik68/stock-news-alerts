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


# ── get_forward_return_from_open: the tradable entry for after-hours news ────
#
# A filing released after 15:30 IST is followed by a close that printed before
# the news existed. get_forward_return() measures from exactly that close, so
# the overnight gap reaction lands inside the number even though no order could
# have been filled at the base. Measured on 271 delivered alerts (2026-09-17):
# the after-hours bucket scored +1.75% avg 1d alpha that way against +0.26% for
# rows with a genuinely tradable base — nearly the whole apparent edge was this
# artefact. Entering at the next open prices the gap out.

def _ohlc(n_days: int, today: date, opens: list[float], closes: list[float]):
    idx = pd.date_range(end=pd.Timestamp(today), periods=n_days, freq="D")
    return pd.DataFrame({"Open": opens, "Close": closes}, index=idx)


def _seed(frame):
    market_data._close_cache.clear()
    market_data._open_cache.clear()
    return patch("yfinance.Ticker", **{"return_value.history.return_value": frame})


def test_open_basis_starts_at_the_next_bar_not_the_news_day_close():
    # Flat 100 closes, but every open is 90 — a gap DOWN to the open. Measuring
    # from the prior close gives 0%; measuring from the tradable open gives
    # +11.1%. The two must not agree, or the gap is still being counted.
    today = date.today()
    n = 60
    frame = _ohlc(n, today, opens=[90.0] * n, closes=[100.0] * n)
    with _seed(frame):
        from_open = market_data.get_forward_return_from_open(
            "G.NS", today - timedelta(days=10), trading_days=1)
    with _seed(frame):
        from_close = get_forward_return("G.NS", today - timedelta(days=10), trading_days=1)

    assert from_close == 0.0
    assert from_open is not None and round(from_open, 1) == 11.1


def _first_bar_after(frame, from_date):
    """Index of the first bar strictly after `from_date` — the entry bar."""
    return next(i for i, ts in enumerate(frame.index) if ts.date() > from_date)


def test_open_basis_spans_the_same_number_of_bars_as_the_close_basis():
    # close of bar i -> close of bar i+N spans N bars; open of bar i+1 -> close
    # of bar i+N must too, or the two horizons aren't comparable.
    today = date.today()
    n = 60
    prices = [100.0 + i for i in range(n)]  # +1 per bar
    frame = _ohlc(n, today, opens=prices, closes=prices)
    from_date = today - timedelta(days=10)
    with _seed(frame):
        ret = market_data.get_forward_return_from_open("H.NS", from_date, trading_days=3)

    b = _first_bar_after(frame, from_date)
    # Held bars b, b+1, b+2 — three sessions, entering at b's open and leaving
    # at b+2's close.
    expected = (frame["Close"].iloc[b + 2] / frame["Open"].iloc[b] - 1) * 100
    assert ret is not None and round(ret, 6) == round(expected, 6)


def test_open_basis_fails_closed_when_the_window_predates_the_news():
    today = date.today()
    frame = _ohlc(20, today, opens=[100.0] * 20, closes=[100.0] * 20)
    with _seed(frame):
        assert market_data.get_forward_return_from_open(
            "I.NS", today - timedelta(days=90), trading_days=3) is None


def test_open_basis_returns_none_when_the_horizon_has_not_matured():
    today = date.today()
    frame = _ohlc(60, today, opens=[100.0] * 60, closes=[100.0] * 60)
    with _seed(frame):
        assert market_data.get_forward_return_from_open(
            "J.NS", today, trading_days=3) is None


def test_open_basis_returns_none_rather_than_nan_when_the_open_is_missing():
    # yfinance leaves a NaN open on a bar that hasn't populated. NaN is a valid
    # float, so an unguarded divide would propagate it into a stored return.
    today = date.today()
    n = 60
    from_date = today - timedelta(days=6)
    frame = _ohlc(n, today, opens=[100.0] * n, closes=[100.0] * n)
    frame.iloc[_first_bar_after(frame, from_date), frame.columns.get_loc("Open")] = float("nan")
    with _seed(frame):
        ret = market_data.get_forward_return_from_open("K.NS", from_date, trading_days=1)
    assert ret is None


def test_close_only_source_still_yields_closes_and_no_opens():
    # A frame without an Open column must not cost us the closes too.
    today = date.today()
    idx = pd.date_range(end=pd.Timestamp(today), periods=60, freq="D")
    frame = pd.DataFrame({"Close": [100.0] * 60}, index=idx)
    with _seed(frame):
        assert get_forward_return("L.NS", today - timedelta(days=10), trading_days=1) == 0.0
        assert market_data.get_forward_return_from_open(
            "L.NS", today - timedelta(days=10), trading_days=1) is None

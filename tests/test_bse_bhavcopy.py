"""Tests for BSE bhavcopy EOD pricing — the source that makes BSE-only scrips
measurable at all (yfinance returns nothing for a large share of them, which
silently left 63-71 alerted rows per horizon permanently unpriceable).

The two traps guarded here were both confirmed against the live endpoint:
  1. A non-trading day returns HTTP 200 with a ~12.5KB HTML page, NOT a 404.
  2. The scrip code lives in `FinInstrmId`, not column 1 and not `TckrSymb`.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.ingestion import bse_bhavcopy
from src.storage.models import Base, BseBhavcopyDay, BseClose

_HEADER = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
    "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,"
    "LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,"
    "TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4"
)


def _row(scrip: str, close: float, isin: str = "INE01BK01022", symbol: str = "AWHCL") -> str:
    cols = [""] * 34
    cols[0] = "2026-07-21"
    cols[5] = scrip          # FinInstrmId
    cols[6] = isin           # ISIN
    cols[7] = symbol         # TckrSymb
    cols[13] = "SOME COMPANY LIMITED"
    cols[17] = str(close)    # ClsPric
    return ",".join(cols)


def _csv(*rows: str) -> str:
    return "\n".join([_HEADER, *rows])


# The real HTML BSE serves for weekends/holidays (truncated).
_HTML_ERROR_PAGE = '<!DOCTYPE html><html lang="en" data-critters-container=""><head>...</head></html>'


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _resp(text: str, status: int = 200) -> Mock:
    r = Mock()
    r.status_code = status
    r.text = text
    return r


# ── parsing ──────────────────────────────────────────────────────────────────

def test_parses_close_keyed_by_fininstrmid_not_tickersymb():
    parsed = bse_bhavcopy._parse(_csv(_row("543254", 446.35)))
    assert parsed == {"543254": (446.35, "INE01BK01022")}


def test_non_trading_day_html_page_is_rejected_not_parsed():
    # HTTP 200 + HTML. Status code alone would wrongly accept this.
    assert bse_bhavcopy._parse(_HTML_ERROR_PAGE) is None


def test_fetch_returns_none_for_html_error_page_despite_http_200():
    with patch.object(bse_bhavcopy.requests, "get", return_value=_resp(_HTML_ERROR_PAGE)):
        assert bse_bhavcopy.fetch_bhavcopy(date(2026, 7, 19)) is None


def test_rows_with_unusable_close_are_skipped():
    parsed = bse_bhavcopy._parse(_csv(_row("1", 0.0), _row("2", 10.0), _row("3", float("nan"))))
    assert "1" not in parsed          # zero close
    assert parsed["2"] == (10.0, "INE01BK01022")


# ── caching ──────────────────────────────────────────────────────────────────

def test_trading_day_is_cached_and_not_refetched(session):
    d = date(2026, 7, 21)
    with patch.object(bse_bhavcopy, "fetch_bhavcopy", return_value={"543254": (446.35, "X")}) as m:
        assert bse_bhavcopy.ensure_day_cached(session, d) is True
        assert bse_bhavcopy.ensure_day_cached(session, d) is True  # second call
    m.assert_called_once()
    assert session.get(BseClose, (d, "543254")).close == 446.35


def test_non_trading_day_negative_is_cached_so_weekend_isnt_refetched(session):
    d = date(2026, 7, 19)  # a Sunday, safely in the past
    with patch.object(bse_bhavcopy, "fetch_bhavcopy", return_value=None) as m:
        assert bse_bhavcopy.ensure_day_cached(session, d) is False
        assert bse_bhavcopy.ensure_day_cached(session, d) is False
    m.assert_called_once()
    assert session.get(BseBhavcopyDay, d).is_trading_day is False


def test_today_negative_is_not_cached_because_eod_publishes_later(session):
    today = datetime.now().date()
    with patch.object(bse_bhavcopy, "fetch_bhavcopy", return_value=None):
        assert bse_bhavcopy.ensure_day_cached(session, today) is False
    # Must stay unrecorded so a later cycle re-checks once BSE publishes.
    assert session.get(BseBhavcopyDay, today) is None


# ── forward return ───────────────────────────────────────────────────────────

def _seed(session, scrip, day_close_pairs):
    for d, c in day_close_pairs:
        session.add(BseClose(trade_date=d, scrip=scrip, close=c))
        session.add(BseBhavcopyDay(trade_date=d, is_trading_day=True))
    session.commit()


def test_forward_return_uses_trading_sessions_not_calendar_days(session):
    base = datetime.now(timezone.utc).date() - timedelta(days=10)
    # Deliberately skip a weekend between the 2nd and 3rd session.
    _seed(session, "543254", [
        (base, 100.0), (base + timedelta(days=1), 110.0), (base + timedelta(days=4), 120.0),
    ])
    with patch.object(bse_bhavcopy, "ensure_day_cached", return_value=True):
        assert bse_bhavcopy.get_forward_return(session, "543254.BO", base, 1) == pytest.approx(10.0)
        assert bse_bhavcopy.get_forward_return(session, "543254.BO", base, 2) == pytest.approx(20.0)


def test_forward_return_none_when_horizon_not_matured(session):
    base = datetime.now(timezone.utc).date() - timedelta(days=3)
    _seed(session, "543254", [(base, 100.0)])
    with patch.object(bse_bhavcopy, "ensure_day_cached", return_value=True):
        assert bse_bhavcopy.get_forward_return(session, "543254.BO", base, 3) is None


def test_forward_return_ignores_non_bse_tickers(session):
    with patch.object(bse_bhavcopy, "ensure_day_cached", return_value=True) as m:
        assert bse_bhavcopy.get_forward_return(session, "RELIANCE.NS", date(2026, 7, 14), 1) is None
    m.assert_not_called()  # no pointless bhavcopy fetches for NSE tickers


def test_prune_drops_only_days_outside_the_window(session):
    old = datetime.now().date() - timedelta(days=90)
    recent = datetime.now().date() - timedelta(days=2)
    _seed(session, "543254", [(old, 100.0), (recent, 110.0)])
    bse_bhavcopy.prune_old_days(session)
    assert session.get(BseClose, (old, "543254")) is None
    assert session.get(BseClose, (recent, "543254")) is not None

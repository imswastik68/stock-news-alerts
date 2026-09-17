"""The alert has to distinguish "material" from "tradable".

Every alert sent is a real filing. Only one bucket survived a date-split
holdout (measured 2026-09-17, 271 delivered alerts, tradable entries only):

  partnership_contract & materiality>=0.75   80.5% all -> 76.9% out-of-sample
  partnership_contract & materiality<0.75    57.1% all -> 52.6% out-of-sample
  other event types    & materiality>=0.75   62.0% all -> 55.0% out-of-sample
  other event types    & materiality<0.75    54.5% all -> 44.1% out-of-sample

These tests pin the rule to that evidence so a later tweak that quietly widens
the validated bucket has to argue with the numbers first.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.scoring.conviction import conviction_line, is_validated_setup, trade_plan

INTRADAY = datetime(2026, 9, 16, 6, 0, tzinfo=timezone.utc)      # Wed 11:30 IST
AFTER_CLOSE = datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc)  # Wed 19:30 IST


# ── the rule needs BOTH halves ───────────────────────────────────────────────

def test_the_validated_bucket_is_recognised():
    assert is_validated_setup("partnership_contract", 0.75) is True
    assert is_validated_setup("partnership_contract", 0.92) is True


def test_right_event_type_but_low_materiality_is_not_validated():
    # 52.6% out-of-sample — the materiality floor is load-bearing, not decoration.
    assert is_validated_setup("partnership_contract", 0.74) is False


def test_high_materiality_on_another_event_type_is_not_validated():
    # 55.0% out-of-sample. Materiality alone does not carry the edge.
    assert is_validated_setup("earnings_surprise", 0.95) is False
    assert is_validated_setup("analyst_rating", 0.99) is False


def test_missing_materiality_does_not_sneak_through():
    assert is_validated_setup("partnership_contract", None) is False
    assert is_validated_setup("partnership_contract", 0.0) is False


# ── what the reader is told ──────────────────────────────────────────────────

def test_validated_alert_is_labelled_and_carries_a_plan():
    line = conviction_line("partnership_contract", 0.8)
    assert "A-setup" in line
    assert trade_plan("partnership_contract", 0.8, INTRADAY) is not None


def test_unvalidated_alert_says_no_edge_and_gets_no_plan():
    # It must not be dressed up as "medium conviction": inventing a ranking over
    # buckets measured at 44-55% out-of-sample is the failure mode here.
    line = conviction_line("analyst_rating", 0.9)
    assert "No measured edge" in line
    assert "A-setup" not in line
    assert trade_plan("analyst_rating", 0.9, INTRADAY) is None


def test_intraday_filing_is_entered_at_the_close():
    plan = trade_plan("partnership_contract", 0.8, INTRADAY)
    assert "CLOSE" in plan


def test_after_hours_filing_is_entered_at_the_next_open_not_the_close():
    # The stale close is exactly the price that made the track record look
    # better than it was; the plan must never point at it.
    plan = trade_plan("partnership_contract", 0.8, AFTER_CLOSE)
    assert "OPEN" in plan
    assert "not capturable" in plan


def test_every_plan_states_the_one_day_exit():
    # The classifier labels 335 of 377 alerts "1_3_days" while the measured edge
    # is gone by day 3 (day1->day3 leg: -0.22%, t=-0.73).
    for published_at in (INTRADAY, AFTER_CLOSE):
        assert "1 day" in trade_plan("partnership_contract", 0.8, published_at)


# ── bearish: the alert must not encourage the obvious trade ──────────────────
#
# Bearish alerts are 21% of volume and come from regulatory_legal /
# earnings_surprise / other — partnership_contract is 118 bullish, 0 bearish, so
# the validated bucket is long-only by construction. On tradable entries the
# short side lost on BOTH views at every horizon (raw is the one a short pays
# out on): 1d 41.2% raw / -0.27%, 3d 46.9% / -1.19%, 5d 46.7% / -0.28%, and
# out-of-sample only 33.3% of them saw the stock fall at all.

def test_bearish_alert_says_do_not_short_not_just_no_edge():
    line = conviction_line("regulatory_legal", 0.9, "bearish")
    assert "Do not short" in line
    assert "A-setup" not in line


def test_bearish_line_quotes_the_raw_hit_rate_not_alpha():
    # Alpha says 47% and raw says 41%; quoting alpha would flatter a trade that
    # only pays out when the stock actually falls.
    line = conviction_line("regulatory_legal", 0.9, "bearish")
    assert "41%" in line and "33%" in line


def test_bearish_never_gets_a_trade_plan():
    for et in ("regulatory_legal", "earnings_surprise", "other", "partnership_contract"):
        assert trade_plan(et, 0.9, INTRADAY) is None or et == "partnership_contract"
    assert conviction_line("earnings_surprise", 0.9, "bearish").startswith("🚫")


def test_bullish_unvalidated_still_gets_the_plain_no_edge_line():
    line = conviction_line("earnings_surprise", 0.9, "bullish")
    assert "No measured edge" in line
    assert "Do not short" not in line


def test_direction_is_optional_so_callers_without_it_still_work():
    assert "No measured edge" in conviction_line("earnings_surprise", 0.9)


# ── alpha is not a win rate ──────────────────────────────────────────────────
#
# The validated hit-rates are ALPHA (beat NIFTY). NIFTY fell in 72% of the 1d
# windows in this sample, so a stock could beat it and still lose money:
# unhedged, an A-setup long won 43.9% of the time at +0.67% avg (t=+1.95).
# "77%" shown alone reads as a win rate and would be traded as one.

def test_a_setup_line_says_the_rate_is_versus_nifty():
    line = conviction_line("partnership_contract", 0.8)
    assert "NIFTY" in line


def test_a_setup_line_also_discloses_the_unhedged_win_rate():
    line = conviction_line("partnership_contract", 0.8)
    assert "44%" in line
    assert "+0.67%" in line


def test_a_setup_line_does_not_present_the_alpha_rate_as_money_made():
    # Guards the specific misread: "77% out-of-sample" with no qualifier.
    line = conviction_line("partnership_contract", 0.8)
    assert "77% out-of-sample" not in line

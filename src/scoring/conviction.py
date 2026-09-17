"""Which alerts actually have a measured edge, and how to trade them.

Every alert this system sends is a real, material filing. That is not the same
as a tradable one. Measured 2026-09-17 on 271 delivered alerts, restricted to
rows whose entry price a trader could really have got (see
market_data.entry_basis_for) and split in half by date so the second half was
never looked at while choosing the rule:

  bucket                          ALL          IN-SAMPLE     OUT-OF-SAMPLE
  partnership_contract, mat>=.75  80.5% +0.84%  86.7% +1.20%  76.9% +0.64%
  partnership_contract, mat<.75   57.1% +0.22%  62.5% +0.61%  52.6% -0.11%
  other event types,    mat>=.75  62.0% -0.05%  66.7% +0.58%  55.0% -0.99%
  other event types,    mat<.75   54.5% +0.12%  71.4% +0.74%  44.1% -0.26%

Only the first bucket survives contact with unseen data. The other three all
look fine in-sample and decay to a coin flip or worse out of it — the last one
most starkly (71.4% -> 44.1%), which is exactly what an overfit subset looks
like and exactly why the split exists.

So this module marks that one bucket and says plainly that the rest have no
demonstrated edge, rather than implying a ranking the data doesn't support.

Holding period is one session. The same alerts decompose per-leg as:
base->day1 +0.70% (t=+3.41), day1->day3 -0.22% (t=-0.73), day3->day5 +0.07%
(t=+0.22). The entire edge is in the first session; after that it is noise that
is, if anything, slightly negative. Holding longer adds variance, not return.
"""

from __future__ import annotations

from src.scoring.market_data import entry_basis_for

# The only combination with an out-of-sample-validated edge. Both halves of the
# rule matter: partnership_contract below the materiality floor drops to 52.6%
# out-of-sample, and high materiality on other event types drops to 55.0%.
VALIDATED_EVENT_TYPE = "partnership_contract"
VALIDATED_MIN_MATERIALITY = 0.75

# Sample size behind the validated bucket, quoted in the alert so the reader can
# weigh it. n=41 total, 26 of them out-of-sample — real, but not large.
_VALIDATED_N = 41
_VALIDATED_OUT_OF_SAMPLE_RATE = "77%"


def is_validated_setup(event_type: str, materiality_score: float | None) -> bool:
    """True only for the one bucket that held up on unseen data."""
    return (
        event_type == VALIDATED_EVENT_TYPE
        and (materiality_score or 0.0) >= VALIDATED_MIN_MATERIALITY
    )


def conviction_line(event_type: str, materiality_score: float | None) -> str:
    """One line telling the reader whether this alert is in the validated bucket
    or merely material. Deliberately blunt about the second case: a 'medium
    conviction' label on a bucket measured at 44-55% out-of-sample would invent
    a signal that isn't there."""
    if is_validated_setup(event_type, materiality_score):
        return (
            f"✅ <b>A-setup</b> — the one bucket with a validated edge "
            f"({_VALIDATED_OUT_OF_SAMPLE_RATE} out-of-sample, n={_VALIDATED_N})"
        )
    return "⚪ No measured edge — material news, but this bucket is ~coin-flip out-of-sample"


def trade_plan(event_type: str, materiality_score: float | None, published_at) -> str | None:
    """Entry and exit for a validated setup, or None for everything else.

    Entry follows the same rule the measurement does, because a plan that
    doesn't match how the edge was measured isn't the edge. News that is public
    before the bell can be entered at that session's close; news released after
    15:30 IST cannot — that close has already printed — so the first real entry
    is the next open, and the overnight gap is simply not available.
    """
    if not is_validated_setup(event_type, materiality_score):
        return None
    if entry_basis_for(published_at) == "next_open":
        entry = "next session's OPEN (after-hours filing — tonight's gap is not capturable)"
    else:
        entry = "today's CLOSE (news is already public, so the close is a real fill)"
    return f"📈 Entry: {entry}\n⏱ Exit: next session's close — the edge is 1 day, gone by day 3"

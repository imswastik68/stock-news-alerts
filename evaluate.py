"""
Track-record report: how good have the alerts actually been?

Reads tracked outcomes (src/scoring/outcomes.py fills the forward returns) and
prints hit-rate + average move + impact rate, broken down by event type and
impact tier, plus precision at a few confidence cutoffs and outcome-tracking
coverage. This is what turns "I think it's good" into "order-win alerts hit
74% [51-89% 95% CI] at +3d over 41 samples, and moved the stock >=2% 60% of
the time."

Two things this file is careful about, both found by actually reading the
numbers rather than trusting the pipeline:

1. Market adjustment. On 2026-07-14 the live alert mix was 66/67 bullish — a
   near-single directional bet — while NIFTY itself moved over the same
   window. A raw forward return conflates "did this specific news call add
   value" with "did the whole market move." src/scoring/outcomes.py now also
   records the NIFTY 50 (^NSEI) forward return over the identical window
   (idx_ret_Nd); alpha = ret_Nd - idx_ret_Nd is what actually answers the
   first question. Raw hit-rate is still reported (comparable to earlier
   history, before alpha existed) alongside alpha hit-rate.
2. Statistical honesty. A 2-sample bucket showing "0%" reads identically to a
   200-sample bucket showing "0%" unless you print the uncertainty too.
   wilson_ci() gives a 95% interval next to every hit-rate so a small bucket
   visibly reads as noise instead of a false signal.

Coverage matters because it isn't 100%: some BSE-only scrips have zero Yahoo
Finance price data and can never be measured (src/ingestion/symbol_master.py
resolves dual-listed names to their priceable NSE ticker to shrink this, but
BSE-exclusive listings remain a real gap) — the coverage section makes that
visible instead of silently excluding them from every other stat.

Run: python evaluate.py [--horizon ret_3d]
"""

from __future__ import annotations

import argparse
import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from src.storage.db import get_session
from src.storage.models import Article

# A move at/above this magnitude counts as "the news actually moved the
# stock," independent of whether our predicted direction was right — this is
# the "was it impactful at all" question, distinct from "did we call it right."
IMPACT_MOVE_THRESHOLD_PCT = 2.0

# How long after publication a horizon is expected to have matured, for the
# coverage check. Matches src/scoring/outcomes.py's own min-age-before-mature
# per horizon so "should have data" means the same thing in both places.
_MIN_AGE_DAYS = {"ret_1d": 1, "ret_3d": 3, "ret_5d": 5}

_Z_95 = 1.959963984540054  # two-sided 95% normal quantile


def wilson_ci(hits: int, n: int, z: float = _Z_95) -> tuple[float, float]:
    """Wilson score 95% confidence interval for a hit-rate (hits out of n).
    More reliable than a plain +/- margin at small n — the case that matters
    most here, since most buckets in this report have well under 30 samples.
    n=0 returns the maximally uninformative (0.0, 1.0)."""
    if n <= 0:
        return (0.0, 1.0)
    p_hat = hits / n
    denom = 1 + z * z / n
    center = p_hat + z * z / (2 * n)
    margin = z * math.sqrt(p_hat * (1 - p_hat) / n + z * z / (4 * n * n))
    lo = (center - margin) / denom
    hi = (center + margin) / denom
    return (max(0.0, lo), min(1.0, hi))


def _fmt_rate_ci(hits: int, n: int) -> str:
    if n == 0:
        return "n/a"
    lo, hi = wilson_ci(hits, n)
    return f"{hits / n:.0%} [{lo:.0%}-{hi:.0%}]"


def _rows(session, horizon):
    ret_col = getattr(Article, horizon)
    idx_col = getattr(Article, f"idx_{horizon}")
    stmt = select(
        Article.event_type, Article.impact_tier, Article.direction,
        Article.confidence, ret_col, idx_col,
    ).where(
        Article.alert_sent == True,  # noqa: E712
        ret_col.is_not(None),
        Article.direction != "neutral",
    )
    return list(session.execute(stmt))


def _hit(direction, ret) -> bool:
    return (direction == "bullish" and ret > 0) or (direction == "bearish" and ret < 0)


def alpha_of(ret: float, idx_ret: float | None) -> float | None:
    """ret minus the NIFTY 50 return over the same window, or None if the
    index leg hasn't been recorded yet for this row (independent fetch, can
    lag behind the stock return — see src/scoring/outcomes.py)."""
    return None if idx_ret is None else ret - idx_ret


def coverage_stats(session, horizon: str) -> dict:
    """Alerted articles old enough that `horizon` should have matured: how many
    actually got a measured outcome vs. stayed NULL (permanently unpriceable —
    mostly BSE-only scrips with no Yahoo Finance data).

    `measured` counts ROWS with a non-NULL value, not "total rows minus unique
    missing tickers" — that set-difference shortcut undercounts missing rows
    (and so overcounts "measured") whenever the same ticker has BOTH a measured
    and an unmeasured alert, which happens constantly here (TATACAP.NS,
    SOMANYCERA.NS, WELCORP.NS etc. each got 2-3 separate alerts)."""
    col = getattr(Article, horizon)
    min_age_days = _MIN_AGE_DAYS.get(horizon, 3)
    cutoff = datetime.now(timezone.utc) - timedelta(days=min_age_days)
    stmt = select(Article.ticker, col).where(
        Article.alert_sent == True,  # noqa: E712
        Article.published_at <= cutoff,
    )
    rows = list(session.execute(stmt))
    total = len(rows)
    measured = sum(1 for _, ret in rows if ret is not None)
    missing_tickers = sorted({ticker for ticker, ret in rows if ret is None})
    return {"total": total, "measured": measured, "missing_tickers": missing_tickers}


def impact_stats(rets: list[float]) -> tuple[float, float]:
    """(avg absolute move %, share of alerts that moved >= IMPACT_MOVE_THRESHOLD_PCT)
    over the given forward-return values — "was the news impactful at all,"
    independent of whether the predicted direction was called correctly."""
    if not rets:
        return 0.0, 0.0
    moves = [abs(r) for r in rets]
    avg_abs_move = sum(moves) / len(moves)
    impact_rate = sum(1 for m in moves if m >= IMPACT_MOVE_THRESHOLD_PCT) / len(moves)
    return avg_abs_move, impact_rate


def _summarize(label, rows):
    if not rows:
        return
    groups: dict[str, list] = {}
    for et, tier, direction, conf, ret, idx_ret in rows:
        groups.setdefault(label(et, tier), []).append((direction, ret, idx_ret))
    print(f"\n{'bucket':<22} {'n':>4} {'hit-rate [95% CI]':<20} {'avg move':>9} {'avg alpha':>10} {'impact%':>8}")
    print("-" * 80)
    for key in sorted(groups):
        items = groups[key]
        n = len(items)
        hits = sum(1 for d, r, _ in items if _hit(d, r))
        avg = sum(r for _, r, _ in items) / n
        avg_abs, impact_rate = impact_stats([r for _, r, _ in items])
        alphas = [a for _, r, idx in items if (a := alpha_of(r, idx)) is not None]
        alpha_str = f"{sum(alphas) / len(alphas):+.2f}%" if alphas else "n/a"
        print(f"{key:<22} {n:>4} {_fmt_rate_ci(hits, n):<20} {avg:>+8.2f}% {alpha_str:>10} {impact_rate:>7.0%}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", default="ret_3d", choices=["ret_1d", "ret_3d", "ret_5d"])
    args = ap.parse_args()

    session = get_session()
    try:
        rows = _rows(session, args.horizon)
        cov = coverage_stats(session, args.horizon)
    finally:
        session.close()

    print(f"=== Alert track record ({args.horizon}) — {len(rows)} matured alert(s) ===")

    if cov["total"] > 0:
        print(
            f"coverage: {cov['measured']}/{cov['total']} matured alerts have measured "
            f"outcomes; {len(cov['missing_tickers'])} unpriceable (mostly BSE-only scrips)"
        )
        if cov["missing_tickers"]:
            print(f"  unpriceable tickers: {', '.join(cov['missing_tickers'][:20])}"
                  + (" ..." if len(cov["missing_tickers"]) > 20 else ""))

    if not rows:
        print("\nNo matured alert outcomes yet. Let it run and re-check in a few days.")
        return

    overall_hits = sum(1 for _, _, d, _, r, _ in rows if _hit(d, r))
    avg_abs_move, impact_rate = impact_stats([r for *_, r, _ in rows])
    print(f"\nOverall raw hit-rate: {_fmt_rate_ci(overall_hits, len(rows))}")
    print(f"Overall avg |move|: {avg_abs_move:.2f}%  |  impactful (>= {IMPACT_MOVE_THRESHOLD_PCT:.0f}% move): {impact_rate:.0%}")

    # Alpha (market-adjusted): only over rows where the index leg has been
    # recorded. If none have it yet (index fetch is independent and can lag),
    # say so plainly rather than printing a misleading 0/0.
    alpha_rows = [(d, alpha_of(r, idx)) for _, _, d, _, r, idx in rows if idx is not None]
    if alpha_rows:
        alpha_hits = sum(1 for d, a in alpha_rows if _hit(d, a))
        avg_alpha = sum(a for _, a in alpha_rows) / len(alpha_rows)
        print(
            f"Alpha hit-rate (vs NIFTY 50): {_fmt_rate_ci(alpha_hits, len(alpha_rows))}"
            f"  (n={len(alpha_rows)}/{len(rows)} have an index leg)"
        )
        print(f"Avg alpha: {avg_alpha:+.2f}%")
    else:
        print("Alpha hit-rate: n/a — no rows have a recorded NIFTY 50 leg yet")

    _summarize(lambda et, tier: et, rows)
    _summarize(lambda et, tier: f"tier:{tier}", rows)

    print("\nPrecision at confidence cutoffs:")
    for cut in (0.55, 0.65, 0.70, 0.80):
        sel = [(d, r) for _, _, d, c, r, _ in rows if c >= cut]
        if sel:
            hits = sum(1 for d, r in sel if _hit(d, r))
            print(f"  conf >= {cut:.2f}: n={len(sel):>4}  hit-rate={_fmt_rate_ci(hits, len(sel))}")


if __name__ == "__main__":
    main()

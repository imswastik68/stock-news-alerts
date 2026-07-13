"""
Track-record report: how good have the alerts actually been?

Reads tracked outcomes (src/scoring/outcomes.py fills the forward returns) and
prints hit-rate + average move + impact rate, broken down by event type and
impact tier, plus precision at a few confidence cutoffs and outcome-tracking
coverage. This is what turns "I think it's good" into "order-win alerts hit
74% at +3d over 41 samples, and moved the stock >=2% 60% of the time."

Coverage matters because it isn't 100%: some BSE-only scrips have zero Yahoo
Finance price data and can never be measured (src/ingestion/symbol_master.py
resolves dual-listed names to their priceable NSE ticker to shrink this, but
BSE-exclusive listings remain a real gap) — the coverage section makes that
visible instead of silently excluding them from every other stat.

Run: python evaluate.py [--horizon ret_3d]
"""

from __future__ import annotations

import argparse
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


def _rows(session, horizon):
    col = getattr(Article, horizon)
    stmt = select(
        Article.event_type, Article.impact_tier, Article.direction,
        Article.confidence, col,
    ).where(
        Article.alert_sent == True,  # noqa: E712
        col.is_not(None),
        Article.direction != "neutral",
    )
    return list(session.execute(stmt))


def _hit(direction, ret):
    return (direction == "bullish" and ret > 0) or (direction == "bearish" and ret < 0)


def coverage_stats(session, horizon: str) -> dict:
    """Alerted articles old enough that `horizon` should have matured: how many
    actually got a measured outcome vs. stayed NULL (permanently unpriceable —
    mostly BSE-only scrips with no Yahoo Finance data)."""
    col = getattr(Article, horizon)
    min_age_days = _MIN_AGE_DAYS.get(horizon, 3)
    cutoff = datetime.now(timezone.utc) - timedelta(days=min_age_days)
    stmt = select(Article.ticker, col).where(
        Article.alert_sent == True,  # noqa: E712
        Article.published_at <= cutoff,
    )
    rows = list(session.execute(stmt))
    total = len(rows)
    missing_tickers = sorted({ticker for ticker, ret in rows if ret is None})
    return {"total": total, "measured": total - len(missing_tickers), "missing_tickers": missing_tickers}


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
    for et, tier, direction, conf, ret in rows:
        groups.setdefault(label(et, tier), []).append((direction, ret))
    print(f"\n{'bucket':<24} {'n':>4} {'hit-rate':>9} {'avg move':>9} {'impact%':>8}")
    print("-" * 60)
    for key in sorted(groups):
        items = groups[key]
        n = len(items)
        hits = sum(1 for d, r in items if _hit(d, r))
        avg = sum(r for _, r in items) / n
        avg_abs, impact_rate = impact_stats([r for _, r in items])
        print(f"{key:<24} {n:>4} {hits / n:>8.0%} {avg:>+8.2f}% {impact_rate:>7.0%}")


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

    overall_hits = sum(1 for _, _, d, _, r in rows if _hit(d, r))
    avg_abs_move, impact_rate = impact_stats([r for *_, r in rows])
    print(f"\nOverall hit-rate: {overall_hits / len(rows):.0%}")
    print(f"Overall avg |move|: {avg_abs_move:.2f}%  |  impactful (>= {IMPACT_MOVE_THRESHOLD_PCT:.0f}% move): {impact_rate:.0%}")

    _summarize(lambda et, tier: et, rows)
    _summarize(lambda et, tier: f"tier:{tier}", rows)

    print("\nPrecision at confidence cutoffs:")
    for cut in (0.55, 0.65, 0.70, 0.80):
        sel = [(d, r) for _, _, d, c, r in rows if c >= cut]
        if sel:
            hits = sum(1 for d, r in sel if _hit(d, r))
            print(f"  conf >= {cut:.2f}: n={len(sel):>4}  hit-rate={hits / len(sel):.0%}")


if __name__ == "__main__":
    main()

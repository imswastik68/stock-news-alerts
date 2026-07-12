"""
Track-record report: how good have the alerts actually been?

Reads tracked outcomes (src/scoring/outcomes.py fills the forward returns) and
prints hit-rate + average move, broken down by event type and impact tier, plus
precision at a few confidence cutoffs. This is what turns "I think it's good" into
"order-win alerts hit 74% at +3d over 41 samples."

Run: python evaluate.py [--horizon ret_3d]
"""

from __future__ import annotations

import argparse

from sqlalchemy import select

from src.storage.db import get_session
from src.storage.models import Article


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


def _summarize(label, rows):
    if not rows:
        return
    groups: dict[str, list] = {}
    for et, tier, direction, conf, ret in rows:
        groups.setdefault(label(et, tier), []).append((direction, ret))
    print(f"\n{'bucket':<24} {'n':>4} {'hit-rate':>9} {'avg move':>9}")
    print("-" * 50)
    for key in sorted(groups):
        items = groups[key]
        n = len(items)
        hits = sum(1 for d, r in items if _hit(d, r))
        avg = sum(r for _, r in items) / n
        print(f"{key:<24} {n:>4} {hits / n:>8.0%} {avg:>+8.2f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", default="ret_3d", choices=["ret_1d", "ret_3d", "ret_5d"])
    args = ap.parse_args()

    session = get_session()
    try:
        rows = _rows(session, args.horizon)
    finally:
        session.close()

    print(f"=== Alert track record ({args.horizon}) — {len(rows)} matured alert(s) ===")
    if not rows:
        print("No matured alert outcomes yet. Let it run and re-check in a few days.")
        return

    overall_hits = sum(1 for _, _, d, _, r in rows if _hit(d, r))
    print(f"Overall hit-rate: {overall_hits / len(rows):.0%}")

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

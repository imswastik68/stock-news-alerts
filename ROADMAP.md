# Roadmap — making this tradable in both directions

Forward-looking plan. `SESSION_LOG.md` is the historical record; this is what's
left to do and why, in priority order. Every claim here is measured against the
production DB (6820 articles, 2026-08-11 → 2026-09-17) — the measuring scripts
and their numbers are named inline so a later session can re-run rather than
re-argue.

Update this file when an item ships or when evidence changes its priority.

---

## The finding that reordered everything — DIAGNOSED AND FIXED 2026-09-17

**77% of ingested filings were never classified.**

| stage | count | note |
|---|---|---|
| articles ingested | 6820 | |
| `classification_failed` | **5223 (77%)** | 5072 of them `impact_tier=high` |
| classified | 1597 | |
| judged material | 1520 | |
| alerted | 377 | |

**Root cause: Gemini's free tier allows 20 requests per day.** Read verbatim
from its own 429 on 2026-09-17:

> Quota exceeded for metric: generate_content_free_tier_requests,
> **limit: 20**, model: gemini-3.5-flash

Against 150–230 filings a day, that is the whole story. It also resolves the
07:00 UTC anomaly this document flagged as "inconsistent with a simple daily
cap": 07:00 UTC is midnight Pacific, when the quota resets. Nothing caught the
overflow because the Groq fallback pointed at a model Groq had decommissioned,
returning 404 on every call — at DEBUG level, under INFO-level logging.

> **Correction.** This section previously said "It is not
> `MAX_ARTICLES_PER_CYCLE`". That was wrong. The cap was applied to the whole
> 48h feed *before* the already-stored check, so its ten slots went to filings
> classified hours earlier. Over a five-hour production run (Actions job
> 105125895353), **all 132 cycles logged `fetched=10` and 85 logged `new=0`**.
> The cap was binding on every single cycle. The reasoning behind the original
> claim — capacity of ~1500 articles/run — was arithmetic about a path the
> articles never took.

Fixed in `77ad76d`: Groq (1000 req/day) leads, dedupe runs before the cap,
failures are retried up to 3× instead of being written off, and the reason is
stored in `classification_error` rather than only in a log that scrolls away.
See `SESSION_LOG.md` for what was verified.

**The re-baseline in P0.4 below is still outstanding and still matters most.**

---

## What the dropped filings contain

Negative catalysts specifically — the category the system looks weakest on.

| negative catalyst | in feed | classified | alerted | bearish |
|---|---|---|---|---|
| credit rating | 568 | 111 | 52 | 8 |
| insolvency / NCLT | 168 | 43 | 10 | 9 |
| resignation / cessation / demise | 84 | 10 | 4 | 4 |
| penalty / SEBI order | 77 | 16 | 8 | 6 |
| fraud / investigation | 48 | 15 | 11 | 8 |
| auditor issues | 22 | 3 | 0 | 0 |
| default / payment delay | 18 | 5 | 5 | 5 |
| **total** | **987** | **203** | **90** | **40** |

**784 negative-catalyst filings were never classified at all.** The intuition
that the system misses bad news is correct, but the cause is throughput, not
blindness — it never reads them.

### The taxonomy also flattened them — FIXED 2026-09-17 (`f925631`)

`EventType` (`src/classification/schema.py`) had no member for credit rating,
management change, insolvency or default, so:

- **Credit rating → `analyst_rating`.** 43 of 46 delivered `analyst_rating`
  alerts are credit-rating filings, not broker opinions. That bucket is
  effectively a credit-rating bucket wearing the wrong name.
- **Insolvency/NCLT → `regulatory_legal`** (22 of 43 as *neutral*).
- **Resignation/demise → `other`**, 5 of 10 *neutral* — so a CEO's death
  produces a neutral "other" and never alerts. This is exactly the failure mode
  worth fixing.

A further problem inside credit rating: most filings are **re-affirmations**
(49 neutral / 43 bullish / 8 bearish). A reaffirmation is a non-event; the
downgrade is the signal, and it is currently buried in the same bucket.

`credit_rating`, `management_change`, `insolvency` and `default_payment` now
exist, and the prompt names the cases the model was getting wrong. The three
bullets above were re-tested live against the new prompt and all three now
classify correctly and directionally. `litigation` and `plant_disruption` were
deliberately left in their existing buckets — see P1.6.

> Note on the `analyst_rating` retirement (2026-09-17): it was measured on what
> is really credit-rating data, and that data is still negative
> (tradable 58.1% hit, **−0.44%** avg alpha, n=31). The retirement stands and
> `credit_rating` inherits it, because that is the population it was measured
> on. Reaffirmations no longer alert, so the bucket can at last be measured on
> rating *actions* alone — see P1.9. Until that is done, a genuine downgrade is
> stored but not pushed.

---

## Why the short side looks broken (and isn't, quite)

Bad news is released after the bell, and the move happens in a gap nobody can
trade.

| direction | alerts | after-hours | share |
|---|---|---|---|
| bullish | 214 | 40 | 19% [14–24] |
| bearish | 57 | 20 | **35%** [24–48] |

Bearish filings are roughly twice as likely to land after the close. And the
bearish edge sits almost entirely in the untradable part:

| bearish, 1d, RAW (what a short banks) | n | stock fell | avg |
|---|---|---|---|
| tradable entry (close) | 34 | 41.2% | **−0.27%** |
| after-hours (gap included) | 17 | 58.8% | **+1.83%** (t=+2.20) |

So the trader intuition is right that the move is real — we just can't enter at
a price that captures it.

### The opening that is left

Splitting after-hours alerts into the untradable gap and the tradable session
after it (live OHLC, `scratchpad/gap_vs_open.py`):

| direction | n | GAP (untradable) | INTRADAY open→close (tradable) |
|---|---|---|---|
| bearish | 16 | 56.2% right, +1.35% | **56.2%, +0.79%** (t=+1.08) |
| bullish | 34 | 70.6% right, +1.59% | 47.1%, **−0.07%** (t=−0.15) |

**Good news is fully priced by the open; bad news keeps drifting down through
the session.** That asymmetry is the most promising untested lead in the system
— and it matches the folk claim that falls persist longer than rises.

n=16. This is a hypothesis, not an edge. It needs roughly 3–4× the sample.

### On "falls are bigger"

Not supported here. Splitting every measured alert by the sign of its own day-1
move: down moves average −2.13% against up moves +2.18% (0.97×), and the fat
tail is on the upside (+20.0%, +15.3% vs −8.5%, −6.9%).

**But "falls last longer" holds up directionally:** after a down day the move
continued 61.3% of the time; after an up day, only 42.7%. Neither is
significant on its own (t=+0.67, t=−0.78), but both point the same way as the
open→close result above.

---

## Plan

### P0 — Fix the funnel. Nothing else matters at 77% loss.

1. ~~**Diagnose the classification failures from the Actions logs.**~~ **DONE** —
   Gemini free tier = 20 requests/day; Groq fallback 404ing on a decommissioned
   model. See above.
2. ~~**Record the failure reason in the DB.**~~ **DONE** — `classification_error`
   column, plus `classify_attempts` so a failure delays a filing instead of
   deleting it.
3. ~~**Fix throughput.**~~ **DONE** — Groq leads (1000 req/day), paced against
   its real 8000 tokens/min ceiling; dedupe before the per-cycle cap. Verified
   live at 10/10 classified per cycle.
4. **Re-baseline everything.** ← **now the top priority.** Every hit-rate in
   `SESSION_LOG.md` is measured on a 23% sample that was *selected by which LLM
   calls happened to succeed* — not a random subsample. Treat current numbers as
   provisional, including the A-setup that the alert currently advertises.
   Needs roughly a fortnight of post-fix history before it says anything.
5. **Batch several filings per call.** Not needed yet, but it is the structural
   answer if ingestion ever widens: ~900 of the ~1700 tokens per call is the
   system prompt, so 8 filings per call is ~2.5× more filings per token. Do this
   before adding a second key.

**Expected:** ~4× the alert candidates, ~3–4× the bearish sample. That alone
makes the open→close question answerable.

### P1 — Give negative catalysts their own identity — DONE 2026-09-17 (`f925631`)

6. ~~**Extend `EventType`.**~~ **DONE** — `credit_rating`, `management_change`,
   `insolvency`, `default_payment` added. `litigation` and `plant_disruption`
   deliberately **not** split: `regulatory_legal` measures 75% at 3d with no
   evidence it is broken, and plant events are too few to justify a bucket.
   Splitting costs sample size, so it needs a reason each time.
7. ~~**Separate rating *actions* from *affirmations*.**~~ **DONE** — the prompt
   forces a reaffirmation to neutral at materiality < 0.3, so it stops entering
   the sample at all. Verified live: a CRISIL downgrade reads bearish/0.80, an
   ICRA reaffirmation neutral/0.15.
8. ~~**Stop neutral-ing genuine catalysts.**~~ **DONE** — verified live on eight
   filing shapes, all eight correct, including the two that must *stay* neutral
   (reaffirmation, end-of-term retirement).
9. **Re-open the `analyst_rating` / `credit_rating` retirement.** Still open, now
   **collecting** (`26db512`). `credit_rating` inherits the retirement because
   the original measurement was taken on that population (43 of 46 delivered
   `analyst_rating` alerts were rating-agency filings). With reaffirmations no
   longer alerting, the bucket can be measured on rating *actions* alone for the
   first time. A genuine downgrade is still stored and not pushed.
   **Read it with `python evaluate.py --shadow --horizon ret_3d`.**
10. **Revisit `min_materiality_score` (0.65) — with measurement, not by feel.**
    In live testing an auditor resignation and a plant fire both scored 0.60, so
    they store but do not push. Both look like real catalysts. Same `--shadow`
    report. Do not move this until P0.4 is done: changing the gate
    mid-re-baseline confounds it.

> **Both of the above were unmeasurable until `26db512`.** `track_outcomes()`
> only ever selected `alert_sent = True`, and a withheld filing never sets that
> flag — so the evidence these two items need was precisely the evidence not
> being collected, and waiting would have produced nothing. Demonstrated live:
> a 3-article cycle classified three filings and withheld all three (one
> `ma_deal`, two `credit_rating`); the alerted pass recorded 0 values, the
> shadow pass recorded 10. Withheld rows keep `alert_sent = False`, so they feed
> no calibration and enter no reported track record — starting to *act* on them
> stays a deliberate decision.

### P2 — Trade the asymmetry, once the sample supports it

11. ~~**Add `ret_intraday` (open→close) as a first-class horizon.**~~
    **NOT NEEDED — it already exists under another name.**

    > **Correction (2026-09-17).** This item assumed open→close was only
    > available via an ad-hoc script. It is not. For a row whose `entry_basis`
    > is `next_open`, `ret_1d` *is* the open→close return of the first session
    > after the news: `get_forward_return_from_open(..., trading_days=1)` enters
    > at `open[next]` and exits at `close[next]` — the same bar. Verified
    > numerically against a constructed OHLC series. `evaluate.py` already
    > breaks every stat down by `entry_basis` and labels tradability, so the
    > "most promising result in this document" is readable today with
    > `python evaluate.py --horizon ret_1d` and reading the `next_open` row.
    > Adding a `ret_intraday` column would duplicate `ret_1d` and create two
    > columns that must agree. What this needs is **sample size, not schema** —
    > which is what P0 and P1 now supply. Caveat: on a `next_close` row
    > (BSE-only scrip, no open price) `ret_1d` is close→close instead, which is
    > exactly why the entry-basis split must not be collapsed.
12. **Re-test bearish open→close at n≥50.** If it holds near +0.79%, bearish
    after-hours alerts get a real trade plan (short at the open, cover at the
    close) instead of today's "do not short this alone". The P0 fix plus the P1
    taxonomy should supply that sample far faster than the old funnel could —
    management_change, insolvency and default_payment are bearish by nature and
    were previously invisible.
13. **Test whether bullish after-hours alerts are worth sending at all.**
    Open→close is −0.07% (n=34): the gap takes everything. If that holds, they
    are informational, not actionable, and should say so.

### P3 — Market-regime risk

14. **The entire sample is a falling market** — NIFTY was down in 72% of 1d
    windows (80% at 3d, 81% at 5d). The A-setup's 80.5% alpha hit-rate against
    a 43.9% raw hit-rate is partly that. No amount of extra data *from this
    window* fixes it; it needs either a rising-market sample or explicit
    market-neutral framing (long stock / short index).

---

## Deliberately not doing

- **Blocking bearish alerts outright.** n=34, CIs spanning 50%, and the
  open→close result suggests the signal is real but mis-entered. Retiring it
  now would delete the evidence needed to fix it.
- **Tuning thresholds on current numbers.** They are measured on an
  LLM-availability-selected 23% of the funnel. Tuning that is fitting noise.
- **Adding more news sources.** The system already ingests 4× what it can
  process. More input makes the bottleneck worse, not better.

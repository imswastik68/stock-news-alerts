# Roadmap — making this tradable in both directions

Forward-looking plan. `SESSION_LOG.md` is the historical record; this is what's
left to do and why, in priority order. Every claim here is measured against the
production DB (6820 articles, 2026-08-11 → 2026-09-17) — the measuring scripts
and their numbers are named inline so a later session can re-run rather than
re-argue.

Update this file when an item ships or when evidence changes its priority.

---

## The finding that reorders everything

**77% of ingested filings are never classified.**

| stage | count | note |
|---|---|---|
| articles ingested | 6820 | |
| `classification_failed` | **5223 (77%)** | 5072 of them `impact_tier=high` |
| classified | 1597 | |
| judged material | 1520 | |
| alerted | 377 | |

Every daily rate since 2026-08-31 sits between 64% and 88%, so this is ongoing,
not a historical blip. The stored reason is uniform: *"LLM classification failed
or backend unreachable."*

This dwarfs every tuning question. The alert quality work to date has been
optimising the 23% of the funnel that survives.

**It is not `MAX_ARTICLES_PER_CYCLE`.** That defaults to 10, but at one cycle
per ~2 min for ~5 h a run can attempt ~1500 articles against ~150–230 arriving
daily. The cap is not binding; the LLM calls themselves are failing.

**Root cause is not yet pinned down.** Failure rate by UTC hour is *not* the
monotonic ramp a pure daily quota would produce — it peaks at 90–95% but drops
to 6% at 07:00 UTC and 28% at 08:00 UTC. That recovery window is inconsistent
with a simple daily cap and needs the actual Actions logs (429 vs timeout vs
parse failure) before anything is changed.

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

### The taxonomy also flattens them

`EventType` (`src/classification/schema.py`) has no member for credit rating,
management change, insolvency, default, litigation or plant disruption. So:

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

> Note on the `analyst_rating` retirement (2026-09-17): it was measured on what
> is really credit-rating data, and that data is still negative
> (tradable 58.1% hit, **−0.44%** avg alpha, n=31). The retirement stands. But
> the right fix is to split the bucket, not to leave downgrades permanently
> blocked alongside reaffirmations.

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

1. **Diagnose the classification failures from the Actions logs.** Count 429 vs
   timeout vs parse failure vs unreachable, per backend. Do not change limits
   before this — the 07:00 UTC recovery says the obvious guess is wrong.
2. **Record the failure reason in the DB.** `reasoning` is one constant string
   for all 5223 rows, which is why this needed log archaeology. A
   `classification_error` column makes it queryable and makes any fix
   verifiable.
3. **Fix throughput** per the diagnosis. Options, cheapest first: a second
   Gemini key; raise `_GEMINI_MIN_INTERVAL_SECONDS` if it's RPM; batch several
   filings per call (a filing classification is ~80 output tokens — 10 per call
   is realistic and cuts request count 10×); make Groq a real fallback rather
   than a same-cycle retry.
4. **Re-baseline everything afterwards.** Every hit-rate in `SESSION_LOG.md` is
   measured on a 23% sample that was *selected by which LLM calls happened to
   succeed* — not a random subsample. Treat current numbers as provisional.

**Expected:** ~4× the alert candidates, ~3–4× the bearish sample. That alone
makes the open→close question answerable.

### P1 — Give negative catalysts their own identity

5. **Extend `EventType`**: `credit_rating`, `management_change`, `insolvency`,
   `default_payment`, `litigation`, `plant_disruption`. Priors in
   `confidence_table.yaml` start conservative and get shrunk toward measurement
   as usual.
6. **Separate rating *actions* from *affirmations*.** A downgrade, an outlook
   cut and a reaffirmation are three different events sharing one category
   today. The prompt must extract the action, not just the topic.
7. **Stop neutral-ing genuine catalysts.** A CEO demise classified
   `other/neutral` never alerts. Add worked examples to `_SYSTEM_PROMPT` for
   management exit, auditor resignation, insolvency admission and rating
   downgrade.
8. **Then re-open the `analyst_rating` retirement** — with credit ratings split
   out and downgrades separated from reaffirmations, the measurement is finally
   asking a coherent question.

### P2 — Trade the asymmetry, once the sample supports it

9. **Add `ret_intraday` (open→close) as a first-class horizon.** Currently the
   only way to see the most promising result in this document is an ad-hoc
   script. It belongs in `outcomes.py` next to `ret_1d`.
10. **Re-test bearish open→close at n≥50.** If it holds near +0.79%, bearish
    after-hours alerts get a real trade plan (short at the open, cover at the
    close) instead of today's "do not short this alone".
11. **Test whether bullish after-hours alerts are worth sending at all.**
    Open→close is −0.07% (n=34): the gap takes everything. If that holds, they
    are informational, not actionable, and should say so.

### P3 — Market-regime risk

12. **The entire sample is a falling market** — NIFTY was down in 72% of 1d
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

# Session Log

Running record of what this project is, what changed recently, and what's
unresolved. Read this first in a new session — before README.md (which is
user-facing docs) and before `git log` (which has no "why still open").

**Update this file at the end of every session that changes code or config
in this repo.** Newest entry on top. Keep entries short: what changed, why,
what's verified, what's still open. Don't repeat what's already stable in
"Current Architecture" below — only put it there once, update it in place
when it changes.

---

## What this project is

Scans Indian stock-exchange filings (NSE/BSE) in real time, classifies them
with an LLM (material event? bullish/bearish? confidence?), and sends
Telegram alerts for the ones likely to move price. Tracks forward returns on
every alert to measure whether the system is actually right, and feeds that
measurement back into a calibrated confidence score. Runs as a GitHub
Actions cron (`news_scan.yml`) plus a periodic self-test
(`evaluate_selftest.yml`).

## Current Architecture

- **Ingestion**: `src/ingestion/exchange_rss.py` (NSE+BSE RSS, official free
  feeds), `nse_announcements.py` (NSE market-wide filings), `pdf_extract.py`
  (reads the actual filing PDF for HIGH-impact categories instead of trusting
  the RSS category tag). `symbol_master.py` resolves company names / BSE
  scrips to NSE tickers (strict exact-match, no fuzzy matching — see its
  module docstring for why). `bse_bhavcopy.py` is the pricing fallback for
  BSE-only scrips with zero Yahoo Finance data.
- **Classification**: `src/classification/classifier.py` — Gemini Flash
  primary, Groq/Ollama fallback. Returns event_type, direction, confidence,
  materiality, impact_horizon.
- **Scoring/gating** (`src/scoring/`): `confidence.py` (Bayesian shrinkage of
  the LLM's confidence toward measured hit-rates per event_type, K=15),
  `source_quality.py` (the alert gate — HIGH tier needs
  `confidence >= high_tier_confidence_threshold`, not an automatic pass;
  `directionally_unreliable_event_types` are blocked outright),
  `priced_in.py` (suppresses bullish alerts when the stock already ran ≥+8%
  in the prior 5 sessions — validated out-of-sample), `dedup.py` (same
  ticker + event_type + direction within 24h = same event, regardless of
  wording), `outcomes.py` (tracks forward returns per alert, yfinance first
  then `bse_bhavcopy` fallback), `market_data.py` (quotes, prior/forward
  return helpers, and `entry_basis_for()` — whether an alert's measured entry
  price was one a trader could actually have been filled at),
  `conviction.py` (the one out-of-sample-validated setup, and the entry/exit
  plan shown in the alert).
- **Pipeline** (`src/pipeline.py`): one cycle = gather articles → dedup →
  classify → score/gate → alert → repair frozen tickers → track outcomes →
  prune cached BSE closes. Every article is wrapped in its own try/except;
  one bad article never aborts the cycle.
- **Config**: `src/config.py` + `confidence_table.yaml` (priors, gate
  thresholds, per-event-type calibration — tune here, not in code).
- **Evaluation**: `evaluate.py` computes the live track record (hit-rate,
  alpha vs index, Wilson CIs, coverage) from the production DB. Run this to
  check "is the system actually working," not the pipeline logs. It also
  breaks the record down by `entry_basis` — if the edge only appears on rows
  whose entry price was never tradable, there is no edge.

## Known structural limits (not bugs, just ceilings)

- `earnings_surprise` event type is weak because there's no consensus
  estimate source — the model can't know what "beat" or "miss" means without
  it. Accept as a ceiling unless a consensus-estimate source shows up.
- BSE-only scrips (no NSE dual-listing, no ISIN cross-match) rely entirely on
  bhavcopy; if BSE ever changes that CSV format silently, those scrips go
  unpriced again with no alarm — worth an occasional spot check.
- NSE archive endpoints (`nsearchives.nseindia.com`) are genuinely flaky.
  The stale-cache fallback in `symbol_master.py` and the retry pass in
  `ticker_repair.py` exist because of this, not hypothetically.

---

## Log

### 2026-09-17 (later) — Most of the measured "edge" was an entry price nobody could trade

**The finding.** `get_forward_return()` measures from the first close on/after
the alert date. For a filing released after 15:30 IST that close printed
*before the news existed*, so the overnight gap reaction was being counted as
alpha — a move no order could ever have caught. Splitting the 271 delivered
alerts by whether their base price was actually reachable:

| entry base | n | 1d alpha hit-rate | avg alpha | t |
|---|---|---|---|---|
| tradable (news public before the base close) | 181 | 63.0% | **+0.26%** | +1.44 |
| contaminated (after-hours filing) | 52 | 69.2% | **+1.75%** | +2.88 |

22% of alerts were carrying most of the reported average alpha, and none of it
was capturable. The honest headline number is +0.26%/day, not +0.59%.

**Fixed**: `market_data.entry_basis_for()` classifies each alert, and
`get_forward_return_from_open()` measures after-hours alerts from the next
session's OPEN (exposure normalised to N bars, so both bases stay comparable).
The index leg follows the same basis — mixing them would hand NIFTY a gap the
stock leg deliberately gave up. New `entry_basis` column records which was
used; BSE-only scrips have no open in bhavcopy so they downgrade to
`next_close`, honest but more conservative. Legacy rows stay NULL and
`evaluate.py` labels them as an upper bound.

**Answering "is it a 1-day trend?" properly.** Cumulative hit-rates across
different row sets can't separate "gave it back" from "different sample", so
this decomposes per-leg on the 207 rows holding all three horizons:

| leg | win% | avg alpha | t |
|---|---|---|---|
| base → day 1 | 66.7% | +0.70% | **+3.41** |
| day 1 → day 3 | 44.4% | −0.22% | −0.73 |
| day 3 → day 5 | 45.9% | +0.07% | +0.22 |

The move does **not** reverse — leg B is not significantly different from zero.
The edge simply *stops* after the first session. Holding longer adds variance,
not return. Conditioning leg B on whether day 1 worked changes nothing
(44.9% vs 43.5%), i.e. no momentum and no mean-reversion to trade.

**Only one bucket survives a holdout** (tradable rows only, split 2026-08-25):

| bucket | ALL | IN | OUT |
|---|---|---|---|
| partnership_contract & mat≥0.75 | 80.5% +0.84% | 86.7% +1.20% | **76.9% +0.64%** |
| partnership_contract & mat<0.75 | 57.1% +0.22% | 62.5% +0.61% | 52.6% −0.11% |
| other event types & mat≥0.75 | 62.0% −0.05% | 66.7% +0.58% | 55.0% −0.99% |
| other event types & mat<0.75 | 54.5% +0.12% | 71.4% +0.74% | 44.1% −0.26% |

The last row (71.4% → 44.1%) is what an overfit subset looks like and is why
the split exists. `src/scoring/conviction.py` encodes only the first bucket;
alerts now carry either "A-setup" with an explicit entry/exit plan, or a blunt
"no measured edge". The plan's entry follows `entry_basis_for()`, so it matches
how the edge was measured. The classifier's own `impact_horizon` is no longer
printed — it says `1_3_days` on 335 of 377 alerts, which the leg table refutes.

**Retired `analyst_rating`** (closes open item 1). Weaker case than `ma_deal`:
its hit-rate is not below chance (54.5% at 1d), so the evidence is average
alpha, negative at every horizon and worsening — tradable-only −0.44% / −1.54%
/ −1.69% at 1d/3d/5d (t = −1.21/−1.88/−1.48), negative in both holdout halves.
No single cut clears p<0.05; six independent cuts lean the same way and it was
~15% of volume. Winning slightly more often while losing more per trade is a
losing alert.

**Verified**: 192 tests green (was 183 + 9 new). `evaluate.py` runs against the
15MB production DB copy and correctly reports every historical row as `legacy`
with the upper-bound warning. Config loads all three retired event types. A bug
the new tests caught before shipping: once a row resolved to `next_close`, the
3d/5d horizons fell through to the close branch and silently re-measured from
the pre-news close — the exact contamination the basis exists to prevent.

**Alpha is not a win rate — and the whole sample is a falling market.** NIFTY
was down in 72% of the 1d windows (80% at 3d, 81% at 5d), averaging −0.14% /
−0.46% / −0.68%. Every hit-rate above is alpha, so a stock could beat NIFTY and
still lose money. For the A-setup, tradable entries:

| view | 1d hit | avg | out-of-sample |
|---|---|---|---|
| alpha (vs NIFTY) | 80.5% | +0.84% | 76.9% |
| **raw (unhedged long)** | **43.9%** | **+0.67%** (t=+1.95) | 38.5%, +0.49% (t=+1.27) |

An A-setup stock averaged +0.67% while NIFTY averaged −0.17% over the same
windows. So the alpha edge is real and the unhedged trade has positive
expectancy, but it is carried by the *size* of winners, not their frequency —
you lose on ~56% of trades. Alerts now print both numbers; "77%" alone reads as
a win rate and would be traded as one. Two consequences: the clean way to
capture the measured edge is market-neutral (long stock / short index), and the
edge has never been tested against a rising index.

**Bearish validated separately — it does not work.** The A-bucket is long-only
by construction, not by choice: `partnership_contract` ran 118 bullish / 0
bearish. Bearish alerts (21% of volume) come from `regulatory_legal` (28),
`earnings_surprise` (14) and `other` (6). Both views are reported because a
short pays out on the stock *falling*, not on it underperforming a rising
index. Tradable entries only:

| horizon | alpha hit | avg alpha | raw hit | avg raw |
|---|---|---|---|---|
| 1d (n=34) | 47.1% | −0.39% | 41.2% | −0.27% |
| 3d (n=32) | 43.8% | −1.65% | 46.9% | −1.19% |
| 5d (n=29) | 37.9% | −0.90% | 46.7% | −0.28% |

Out-of-sample it degrades further: 44.4% alpha, **33.3% raw** — only a third of
held-out bearish calls saw the stock fall at all. Across ALL delivered rows
bearish looks mildly *positive* (+0.43% raw at 1d), but 20 of those 57 were
after-hours filings measured from a pre-news close; removing the uncapturable
entries flips the sign. Bearish alerts now carry an explicit "🚫 Do not short
this alone" line quoting the raw hit-rate. Not blocked outright — n=34 with CIs
spanning 50% is not grounds for retirement, and the news itself is real.

**Open**:
1. The A-setup bucket is n=41 (26 out-of-sample). Real, but small — worth
   re-checking around **2026-10-15** once ~4 more weeks have matured.
1b. Bearish: re-check at n≈70 (~**2026-11**). If the raw hit-rate stays near
   40%, retire the direction claim the way `analyst_rating` was.
2. Every historical row is still `entry_basis` NULL and can't be recomputed
   without writing to the production DB in the Actions cache. They age out of
   the 20-day tracking window on their own; the first fully-honest read arrives
   around **2026-10-07**.
3. Volume after retiring `analyst_rating` should drop ~15% (≈6.2/day). If the
   A-setup rate stays near 1/day, the useful signal is a small fraction of what
   gets sent — consider whether the non-A alerts are worth sending at all.
4. Still unaddressed from the entry below: the `suppressed_reason` backfill,
   which needs the same production-DB write authorization.

### 2026-09-17 — First clean track-record read; fixed a blind selftest and a diluted metric

**The selftest had been measuring nothing for five weeks.** `actions/cache`
derives a cache *version* from the path list, so a restore whose paths differ
from the save step matches nothing even when the key prefix is right. 53bc0cc
expanded `news_scan.yml` to save four paths (DB + three symbol caches) while
`evaluate_selftest.yml` kept restoring `stock_news.db` alone. Every run since
2026-08-12 printed "No cached DB found" and `exit 0`'d — green, reporting
nothing. Path lists now match, and a missing DB fails the job loudly.

**Withheld alerts were being counted as alerts.** Suppressed duplicates and
priced-in rows call `mark_alert_sent` so the pending queue never retries them,
but `alert_sent` was also how `evaluate.py` and `get_hit_rate_stats` decided an
alert went out. 106 of 377 rows (28%) had no matching Telegram message. Added
`suppressed_reason` (NULL = really delivered); measurement and calibration now
filter on it. Outcome tracking still fills returns for suppressed rows on
purpose, so the counterfactual stays available.

**Track record (377 alerts, 2026-08-11 to 2026-09-17, all post-gate-fix —
the first read where no pre-fix row is mixed in). Delivered-only, alpha vs
NIFTY 50:**

| horizon | n | alpha hit-rate | avg alpha |
|---|---|---|---|
| 1d | 233 | **64.4%** [58.0–70.2] | +0.59% |
| 3d | 220 | 48.6% [42.1–55.2] | +0.36% |
| 5d | 207 | 47.8% [41.1–54.6] | +0.54% |

**The edge is real but it lives entirely at 1 day and is gone by day 3.**
Confirmed on a time-split holdout (split 2026-08-24, second half untouched):

- `partnership_contract`: 69.6% in-sample → **67.5% out-of-sample** (n=77).
  Holds. With materiality ≥0.75: 84.0% → 76.2% (n=42). Above 50% in 6 of 7
  weeks individually, so not one lucky week.
- `analyst_rating`: 46.7% → 52.8%, negative avg alpha in both halves. No edge.
- **Confidence does not rank.** `conf ≥ 0.55` went 62.4% in-sample → 40.9%
  out-of-sample, and is *worse* than taking everything (57.9% vs 59.8%).
  Materiality ranks better and holds up (mat ≥0.75 → 63.9% at 1d).

**Open / recommended, not yet done** (deliberately left as decisions).
*Superseded by the entry above: 1, 2 and 3 were actioned on 2026-09-17 —
`analyst_rating` is retired, `impact_horizon` is no longer shown in alerts, and
conviction is now decided by `event_type × materiality` rather than confidence.
4 is still open.*
1. Retire `analyst_rating` the way `ma_deal` was — it is 15% of volume for no
   measured edge.
2. Alerts advertise `impact_horizon: 1_3_days` (335 of 377) but the measured
   edge is 1d only. Either the label or the holding advice is wrong.
3. Confidence is not earning its keep as a ranker. Consider ranking on
   materiality, or gating on `event_type × materiality` instead.
4. Historical rows predate `suppressed_reason`, so they are all NULL and still
   dilute the numbers above until they age out. The clean-vs-diluted gap is
   known (64.4% vs 59.8% at 1d); a backfill would mean writing to the
   production DB in the Actions cache, so it was not done unilaterally.

### 2026-08-12 — Reconnect confidence gate, retire ma_deal, add priced-in filter, stop losing tickers
Biggest change to date. Root cause found: HIGH-impact filings were alerting
regardless of confidence (`source_quality.py` returned `True` unconditionally
for HIGH tier), so 94% of alerts bypassed calibration entirely. Fixed by
requiring `confidence >= high_tier_confidence_threshold` (0.35) even for HIGH
tier, and outright blocking `directionally_unreliable_event_types`
(`ma_deal`, `insider_activity` — both measured below chance, `ma_deal` was
0-for-35, p≈4e-6). Re-tuned `confidence_table.yaml` priors from measured
production hit-rates instead of hand-tuned guesses.

Added `src/scoring/priced_in.py`: suppress bullish alerts when the stock
already ran ≥+8% in the prior 5 sessions (measured 22% hit-rate / -3.09%
alpha above threshold vs. 36% / -0.32% below; validated out-of-sample,
in-sample -3.07% vs out-of-sample -3.12%, so not overfit).

Added `src/ingestion/bse_bhavcopy.py`: official BSE daily EOD CSV as a
pricing fallback for the ~63-71 BSE scrips yfinance has zero data for. Two
guarded traps: non-trading days return HTTP 200 with an HTML error page (not
a 404), and the scrip-code column isn't always column 1.

Fixed `dedup.py`: was only catching figure-free restatements; NESTLEIND
alerted 3 times in 13 minutes for the same Q1 filing because each mention
used a different metric (profit vs. sales growth), so text similarity never
crossed 0.65. Switched to ticker+event_type+direction match within 24h,
independent of wording.

Fixed a symbol-resolution freeze: a transient NSE archive outage during
ingest permanently stored a company name as the ticker for 136 production
rows (no retry mechanism existed). Fixed in three layers: stale-cache
fallback in `symbol_master.py` (an expired symbol list beats none), symbol
caches now persist across GitHub Actions runs (`news_scan.yml` cache keys
expanded), and `src/ingestion/ticker_repair.py` retries any row still inside
the 20-day tracking window. 106 of 136 frozen rows resolved on replay.

Fixed a unit bug in the classifier prompt: Nestle Q1 profit was alerted at
10x its real value because the filing reported figures in "Rs. in million"
and the model copied the digits across while labeling them crore. Added an
explicit unit-conversion block with a worked example to
`_SYSTEM_PROMPT` in the classifier.

**Verified**: 162 tests green. Live measurement: 3d alpha 33% [28-40] → 43%
[36-49] (p=0.013), coverage 63% → 90%, alert volume 16.3 → 10.2/day (more
selective, not just fewer), zero post-fix duplicates (21 pre-fix → 0),
5 previously-unpriceable BSE scrips now return real forward returns.
Deployed as commit `53bc0cc`.

**Open**: Re-run `evaluate-selftest` around **2026-08-26** (~2 weeks out) for
the first track-record read where every alert matured entirely under the new
gate — everything checked so far mixes pre-fix and post-fix rows by
necessity (post-fix sample was still young). If `earnings_surprise` is still
weak at that point, it's the structural no-consensus-estimate ceiling above,
not a new bug.

### Earlier (pre-2026-08-12, see `git log` for exact commits)
- Added Gemini Flash as primary classifier backend (free, no Groq TPM
  ceiling); Groq/Ollama fallback.
- Added BSE+NSE official RSS ingestion (BSE's JSON API blocks scripted
  access; RSS is the free path).
- Added outcome tracking + Bayesian-calibrated confidence + `evaluate.py`
  track-record reporting.
- Fixed NaN leaking into alert text (`₹nan | ▼nan%`) — filter at both the
  quote source and the display layer.
- Fixed BSE tickers like `532933.BO` getting Telegram-autolinkified and
  splitting the bold alert title — display as `532933 (BSE)` in alert text
  only, real ticker unchanged everywhere else.
- Added cross-source duplicate suppression (dual-listed NSE+BSE filing of
  the same event).
- Raised `track_outcomes()` batch limit 15 → 60 — the real cause of a batch
  of large-caps misreported as "unpriceable" was a processing backlog, not a
  data-availability gap.
- Removed a dormant watchlist/media subsystem (google_news, newsapi,
  indian_rss, materiality_filter, per-symbol NSE polling) — market-wide
  NSE-filings-only is the whole design now.

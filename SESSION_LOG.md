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
  return helpers).
- **Pipeline** (`src/pipeline.py`): one cycle = gather articles → dedup →
  classify → score/gate → alert → repair frozen tickers → track outcomes →
  prune cached BSE closes. Every article is wrapped in its own try/except;
  one bad article never aborts the cycle.
- **Config**: `src/config.py` + `confidence_table.yaml` (priors, gate
  thresholds, per-event-type calibration — tune here, not in code).
- **Evaluation**: `evaluate.py` computes the live track record (hit-rate,
  alpha vs index, Wilson CIs, coverage) from the production DB. Run this to
  check "is the system actually working," not the pipeline logs.

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

**Open / recommended, not yet done** (deliberately left as decisions):
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
